#!/usr/bin/env bash
# Module 6 (GPU host): start the course CA (step-ca), allow one-year certificates, and issue the
# gateway's server certificate.
#
#   source model-host/env.sh && ./model-host/pki/init-ca.sh
#
# Idempotent: an existing CA (volume taco-pki-data) and gateway certificate are kept; pass --reissue to
# replace the gateway certificate. Everything secret lands in model-host/secrets/pki (git-ignored).
set -euo pipefail
: "${MODEL_HOST_DIR:?source model-host/env.sh first}"
cd "$MODEL_HOST_DIR"

REISSUE=0
[ "${1:-}" = "--reissue" ] && REISSUE=1
GATEWAY_DIR="${PKI_DIR}/gateway"
mkdir -p "$GATEWAY_DIR"
chmod 700 "$PKI_DIR"

if [ ! -s "${PKI_DIR}/password" ]; then
  (umask 077; head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32 > "${PKI_DIR}/password")
  echo "Created ${PKI_DIR}/password (CA key and provisioner password)."
fi

wait_healthy() {
  for _ in $(seq 1 60); do
    if podman exec step-ca step ca health 2>/dev/null | grep -q ok; then return 0; fi
    sleep 1
  done
  echo "step-ca did not become healthy; check: podman logs step-ca" >&2
  exit 1
}

if ! podman volume exists taco-pki-data || ! podman run --rm -v taco-pki-data:/home/step "$STEP_CLI_IMAGE" \
    test -f /home/step/config/ca.json; then
  echo "Initializing the course CA (first start)..."
  podman rm -f step-ca >/dev/null 2>&1 || true
  # The password is only needed for `step ca init`; the container is recreated without it below.
  STEP_CA_INIT_PASSWORD="$(cat "${PKI_DIR}/password")" podman compose up -d step-ca
  wait_healthy
  # Course decision 54: every certificate is valid for one year (step-ca's default maximum is 24 h).
  podman exec step-ca step ca provisioner update "$STEP_PROVISIONER" \
    --x509-min-dur=5m --x509-default-dur=8760h --x509-max-dur=8760h
  podman rm -f step-ca >/dev/null
fi
podman compose up -d step-ca
wait_healthy

podman exec step-ca cat /home/step/certs/root_ca.crt > "${PKI_DIR}/root_ca.crt"
cp "${PKI_DIR}/root_ca.crt" "${GATEWAY_DIR}/root_ca.crt"
[ -f "${GATEWAY_DIR}/revoked-serials.map" ] || \
  printf '# Rotated-out client certificate serials (hex, as NGINX reports them): "<SERIAL>" 1;\n' \
    > "${GATEWAY_DIR}/revoked-serials.map"

if [ "$REISSUE" = 1 ] || [ ! -s "${GATEWAY_DIR}/gateway.crt" ]; then
  echo "Issuing the gateway certificate for ${SLM_HOSTNAME}..."
  podman run --rm --user 0 --network taco-model-inference -v "${PKI_DIR}:/pki" "$STEP_CLI_IMAGE" \
    step ca certificate "$SLM_HOSTNAME" /pki/gateway/gateway.crt /pki/gateway/gateway.key \
      --san "$SLM_HOSTNAME" --san "$MODEL_HOST" --san localhost \
      --ca-url https://step-ca:9000 --root /pki/root_ca.crt \
      --provisioner "$STEP_PROVISIONER" --provisioner-password-file /pki/password \
      --not-after 8760h --kty EC --force
fi

echo
podman run --rm -v "${GATEWAY_DIR}:/c:ro" "$STEP_CLI_IMAGE" step certificate inspect /c/gateway.crt --short
echo
echo "Root fingerprint: $(podman exec step-ca step certificate fingerprint /home/step/certs/root_ca.crt)"
echo
echo "Next, on the desktop (repository root), copy the root certificate and the provisioner password:"
echo "  scp <gpu-host>:${PKI_DIR}/root_ca.crt secrets/pki/root_ca.crt"
echo "  scp <gpu-host>:${PKI_DIR}/password    secrets/pki/provisioner_password"
echo "Then start the secure gateway here:  podman compose stop gateway && podman compose up -d gateway-secure"

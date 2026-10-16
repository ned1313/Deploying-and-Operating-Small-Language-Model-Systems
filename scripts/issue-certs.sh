#!/usr/bin/env bash
# Module 6 (desktop, WSL): issue certificates from the course CA on the GPU host into secrets/certs/.
#
#   scripts/issue-certs.sh                       # complaint-api, tools, prometheus (clients), app-gateway (server)
#   scripts/issue-certs.sh tools                 # one identity
#   scripts/issue-certs.sh --rotate complaint-api   # new key and certificate; prints the old serial to deny
#   scripts/issue-certs.sh --show                # subject, serial, and expiry of every issued certificate
#
# Needs secrets/pki/root_ca.crt and secrets/pki/provisioner_password, copied from the GPU host after
# model-host/pki/init-ca.sh, and MODEL_HOST (environment, or observability/.env). Planning decision 55b:
# the desktop holds the provisioner password, so it can issue any identity; acceptable for the demo only.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS="${TACO_SECRETS_DIR:-${REPO}/secrets}"
STEP_CLI_IMAGE="${STEP_CLI_IMAGE:-docker.io/smallstep/step-cli:0.31.0}"
CA_HOSTNAME="${CA_HOSTNAME:-ca.taco.internal}"
CA_PORT="${CA_PORT:-9000}"
PROVISIONER="${STEP_PROVISIONER:-taco}"
CLIENTS=(complaint-api tools prometheus)
if [ -z "${MODEL_HOST:-}" ] && [ -f "${REPO}/observability/.env" ]; then
  MODEL_HOST="$(sed -n 's/^MODEL_HOST=//p' "${REPO}/observability/.env" | tail -1)"
fi
: "${MODEL_HOST:?set MODEL_HOST (the GPU host address) or add it to observability/.env}"
for file in pki/root_ca.crt pki/provisioner_password; do
  [ -s "${SECRETS}/${file}" ] || { echo "missing secrets/${file}: copy it from the GPU host (see m6/README.md)" >&2; exit 1; }
done

step() {
  podman run --rm --user 0 --add-host "${CA_HOSTNAME}:${MODEL_HOST}" -v "${SECRETS}:/secrets" "$STEP_CLI_IMAGE" step "$@"
}

serial_hex() {  # NGINX reports $ssl_client_serial as upper-case hex
  step certificate inspect "/secrets/$1" --format json | grep -o '"serial_number": *"[0-9]*"' | grep -o '[0-9]\+' |
    python3 -c 'import sys; v = int(sys.stdin.read()); h = format(v, "X"); print(h if len(h) % 2 == 0 else "0" + h)'
}

issue() {
  local id="$1" dir="certs/$1"
  mkdir -p "${SECRETS}/${dir}"
  local common=(--ca-url "https://${CA_HOSTNAME}:${CA_PORT}" --root /secrets/pki/root_ca.crt
                --provisioner "$PROVISIONER" --provisioner-password-file /secrets/pki/provisioner_password
                --not-after 8760h --kty EC --force)
  if [ "$id" = app-gateway ]; then
    step ca certificate app.taco.internal "/secrets/${dir}/tls.crt" "/secrets/${dir}/tls.key" \
      --san app.taco.internal --san auth.taco.internal --san localhost "${common[@]}"
  else
    step ca certificate "$id" "/secrets/${dir}/tls.crt" "/secrets/${dir}/tls.key" "${common[@]}"
  fi
  cp "${SECRETS}/pki/root_ca.crt" "${SECRETS}/${dir}/root_ca.crt"
  # Readable by non-root users inside the containers (Prometheus runs as nobody, and the folder is mounted
  # whole): the folder can be traversed but not listed. Demo trade-off; see secrets/README.md.
  chmod 711 "${SECRETS}/${dir}"
  chmod 644 "${SECRETS}/${dir}/tls.crt" "${SECRETS}/${dir}/tls.key" "${SECRETS}/${dir}/root_ca.crt"
  echo "  ${id}: serial $(serial_hex "${dir}/tls.crt")"
}

show() {
  for dir in "${SECRETS}"/certs/*/; do
    [ -s "${dir}tls.crt" ] || continue
    local id; id="$(basename "$dir")"
    echo "${id}: $(step certificate inspect "/secrets/certs/${id}/tls.crt" --short | tr '\n' ' ')"
  done
}

case "${1:-}" in
  --show) show ;;
  --rotate)
    id="${2:?usage: issue-certs.sh --rotate <identity>}"
    [ -s "${SECRETS}/certs/${id}/tls.crt" ] || { echo "no certificate for ${id} yet" >&2; exit 1; }
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "${SECRETS}/rotated"
    old="rotated/${id}-${stamp}"
    cp "${SECRETS}/certs/${id}/tls.crt" "${SECRETS}/${old}.crt"
    cp "${SECRETS}/certs/${id}/tls.key" "${SECRETS}/${old}.key"
    old_serial="$(serial_hex "${old}.crt")"
    echo "Rotating ${id} (old serial ${old_serial}):"
    issue "$id"
    # Passive revocation: the CA will not renew the old certificate. NGINX still accepts it until denied.
    step ca revoke --cert "/secrets/${old}.crt" --key "/secrets/${old}.key" \
      --ca-url "https://${CA_HOSTNAME}:${CA_PORT}" --root /secrets/pki/root_ca.crt --reason "rotated" || true
    echo
    echo "The ${id} container picks up the new files on its next connection. Old certificate kept in secrets/${old}.crt."
    echo "End the rotation window on the GPU host (wait a minute for open connections to recycle):"
    echo "  ./model-host/pki/deny-serial.sh ${old_serial} ${id}-rotation"
    ;;
  "") echo "Issuing from https://${CA_HOSTNAME}:${CA_PORT} (${MODEL_HOST}):"
      mkdir -p "${SECRETS}/ca"
      cp "${SECRETS}/pki/root_ca.crt" "${SECRETS}/ca/root_ca.crt"   # public: for clients that only verify servers
      chmod 644 "${SECRETS}/ca/root_ca.crt"
      for id in "${CLIENTS[@]}" app-gateway; do issue "$id"; done ;;
  *) for id in "$@"; do issue "$id"; done ;;
esac

#!/usr/bin/env bash
# Module 6 (GPU host): end a rotation window. Rejects a rotated-out client certificate at the gateway
# immediately (step-ca's revocation only stops renewal; the certificate itself stays valid for a year).
#
#   source model-host/env.sh && ./model-host/pki/deny-serial.sh <HEX-SERIAL> [note]
#
# scripts/issue-certs.sh --rotate on the desktop prints the serial to use.
set -euo pipefail
: "${PKI_DIR:?source model-host/env.sh first}"
serial="$(printf '%s' "${1:?usage: deny-serial.sh <HEX-SERIAL> [note]}" | tr -d ':' | tr '[:lower:]' '[:upper:]')"
[[ "$serial" =~ ^[0-9A-F]+$ ]] || { echo "not a hex serial: $1" >&2; exit 2; }
note="${2:-rotated}"
map="${PKI_DIR}/gateway/revoked-serials.map"
if grep -q "^\"${serial}\"" "$map" 2>/dev/null; then
  echo "${serial} is already denied."
else
  printf '"%s" 1;  # %s %s\n' "$serial" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$note" >> "$map"
  echo "Denied ${serial}."
fi
podman exec gateway-secure nginx -t -q && podman exec gateway-secure nginx -s reload
echo "gateway-secure reloaded."

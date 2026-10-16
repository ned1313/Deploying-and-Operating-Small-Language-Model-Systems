"""Demo 6.1: probe the secured model gateway and compare every answer with the expected one.

    podman-compose run --rm gateway-check
    podman-compose run --rm gateway-check --extra-cert old/tls.crt old/tls.key 401   # a rotated-out certificate

Runs from a tools container with the course root CA and the `tools` client certificate. Prometheus'
own certificate is exercised by Prometheus itself (`up{job="vllm"}`); this check shows that the tools
identity cannot read metrics. Exit status 1 when any answer differs from the expectation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import ssl
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from taco_shared.reporting import timestamp_slug
from taco_shared.tls import TlsFiles

RESULTS = Path(os.environ.get("RESULTS_DIR", "/app/results/security"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get("INFERENCE_BASE_URL", ""),
                        help="https://slm.taco.internal:8443/v1 (default: INFERENCE_BASE_URL)")
    parser.add_argument("--plain-port", type=int, default=int(os.environ.get("GATEWAY_PLAIN_PORT") or 8080),
                        help="the modules 1-5 HTTP port, expected to be closed")
    parser.add_argument("--model", default=os.environ.get("INFERENCE_MODEL") or "",
                        help="served name for a one-token chat completion (skipped when empty)")
    parser.add_argument("--burst", type=int, default=120, help="concurrent requests for the rate-limit probe")
    parser.add_argument("--extra-cert", nargs=3, metavar=("CERT", "KEY", "STATUS"), action="append", default=[],
                        help="also call GET /v1/models with this certificate and expect STATUS")
    return parser.parse_args()


def self_signed(directory: Path) -> tuple[str, str]:
    """A client certificate for CN=tools that the course CA did not sign."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tools")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=1))
            .not_valid_after(now + dt.timedelta(hours=1)).sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "self.crt", directory / "self.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return str(cert_path), str(key_path)


def context(ca_file: str | None, cert: tuple[str, str] | None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=ca_file)
    if cert:
        ctx.load_cert_chain(*cert)
    return ctx


def request(root: str, ctx: ssl.SSLContext | bool, method: str, path: str, **kwargs: Any) -> str:
    """HTTP status as a string, or the transport error class (for TLS failures)."""
    try:
        with httpx.Client(verify=ctx, timeout=30.0) as client:
            return str(client.request(method, root + path, **kwargs).status_code)
    except httpx.HTTPError as error:
        cause = error.__cause__ or error
        return "tls_error" if isinstance(cause, ssl.SSLError) or "CERTIFICATE_VERIFY" in str(error) else type(error).__name__


def port_closed(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=3):
            return "open"
    except OSError:
        return "closed"


def main() -> int:
    args = parse_args()
    files = TlsFiles.from_env()
    if not args.base_url.startswith("https://") or not files.ca_file or not files.cert_file:
        sys.exit("needs INFERENCE_BASE_URL=https://..., INFERENCE_CA_FILE, and INFERENCE_CLIENT_CERT_FILE/KEY_FILE "
                 "(module 6 settings in tools/.env)")
    root = args.base_url.rstrip("/").removesuffix("/v1")
    host = urlsplit(root).hostname or ""
    tools = context(files.ca_file, (files.cert_file, files.key_file or files.cert_file))
    anonymous = context(files.ca_file, None)
    checks: list[tuple[str, str, str]] = []

    def check(name: str, expected: str, actual: str) -> None:
        checks.append((name, expected, actual))

    check(f"plain HTTP port {args.plain_port}", "closed", port_closed(host, args.plain_port))
    check("GET /health, no client cert", "200", request(root, anonymous, "GET", "/health"))
    check("GET /version, no client cert", "200", request(root, anonymous, "GET", "/version"))
    check("GET /v1/models, no client cert", "401", request(root, anonymous, "GET", "/v1/models"))
    with tempfile.TemporaryDirectory() as scratch:
        foreign = context(files.ca_file, self_signed(Path(scratch)))
        check("GET /v1/models, cert not signed by the course CA", "401", request(root, foreign, "GET", "/v1/models"))
    check("GET /v1/models, tools cert", "200", request(root, tools, "GET", "/v1/models"))
    if args.model:
        body = {"model": args.model, "messages": [{"role": "user", "content": "Reply with: ok"}], "max_tokens": 1}
        check("POST /v1/chat/completions, tools cert", "200",
              request(root, tools, "POST", "/v1/chat/completions", json=body))
    check("GET /metrics, tools cert (inference identity)", "403", request(root, tools, "GET", "/metrics"))
    check("GET /metrics/node, tools cert", "403", request(root, tools, "GET", "/metrics/node"))
    check("POST /v1/completions, tools cert (not allowlisted)", "404",
          request(root, tools, "POST", "/v1/completions", json={}))
    check("POST /tokenize, tools cert (not allowlisted)", "404", request(root, tools, "POST", "/tokenize", json={}))
    check("POST /v1/chat/completions, 300 KB body", "413",
          request(root, tools, "POST", "/v1/chat/completions", content=b"x" * 300_000,
                  headers={"Content-Type": "application/json"}))
    for cert, key, status in args.extra_cert:
        check(f"GET /v1/models, {cert}", status, request(root, context(files.ca_file, (cert, key)), "GET", "/v1/models"))
    with ThreadPoolExecutor(max_workers=min(args.burst, 64)) as pool:
        statuses = list(pool.map(lambda _: request(root, tools, "GET", "/v1/models"), range(args.burst)))
    limited = statuses.count("429")
    check(f"{args.burst} concurrent GET /v1/models, tools cert", "some 429",
          "some 429" if limited else f"no 429 ({statuses.count('200')} x 200)")
    check("GET /v1/models without the course root CA", "tls_error",
          request(root, True, "GET", "/v1/models"))

    width = max(len(name) for name, _, _ in checks)
    failed = 0
    for name, expected, actual in checks:
        ok = expected == actual
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  expected {expected:<9} got {actual}")
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"gateway-check-{timestamp_slug()}.json"
    path.write_text(json.dumps({"base_url": args.base_url, "rate_limited": limited, "checks": [
        {"check": n, "expected": e, "actual": a, "passed": e == a} for n, e, a in checks]}, indent=2) + "\n")
    print(f"\n{len(checks) - failed}/{len(checks)} as expected; wrote {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

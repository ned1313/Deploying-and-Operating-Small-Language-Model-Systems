# Demo 6.1: Secure the request path

Goal: put the model behind TLS with a client certificate per service, restrict routes, sizes, and rates;
move Prometheus to the authenticated metrics route without losing history; rotate a credential; then
show that the application, not the gateway, decides which stores and cases each person may act on.

Prerequisites: modules 1-5 working; the module 6 setup in [README.md](README.md). **GPU host** commands
run from the repository root after `source model-host/env.sh`; desktop commands run in WSL from the
folder named in each step.

---

## Clip 2: adding authentication and access controls

### 1. GPU host: keep the exporters off the LAN

Create `model-host/local.env` (git-ignored; `env.sh` reads it):

```bash
cat > model-host/local.env <<'EOF'
# Module 6: only gateway-secure reaches the GPU exporter.
export GPU_EXPORTER_BIND=127.0.0.1
export NODE_EXPORTER_UPSTREAM=host.containers.internal:9100
EOF
source model-host/env.sh
podman rm -f node-exporter dcgm-exporter && podman compose up -d node-exporter dcgm-exporter
```

The course GPU host runs rootless Podman, on a rootful host you can add
`export NODE_EXPORTER_LISTEN=10.89.7.1` to `local.env` to take it off the LAN as well.

### 2. GPU host: the course CA and the gateway certificate

```bash
chmod +x model-host/pki/*.sh
./model-host/pki/init-ca.sh
```

The first run starts `step-ca`, lets its provisioner issue one-year certificates, exports the root
certificate, and issues the gateway's server certificate for `slm.taco.internal`. It prints the root
fingerprint and the two `scp` commands for the desktop. Show the server certificate:

```bash
podman run --rm --user 0 -v "$PKI_DIR/gateway:/c:ro" "$STEP_CLI_IMAGE" step certificate inspect /c/gateway.crt --short
```

### 3. GPU host: swap the gateway

```bash
podman compose stop gateway
podman compose up -d gateway-secure
ss -tlnp | grep -E ':(8000|8080|8443|9000|9100|9400)\b'
```

Expected: 8443, 9000, and 9100 on `MODEL_HOST`, 8000 and 9400 on 127.0.0.1, and nothing on 8080
(9100 on 10.89.7.1 on a rootful host). Check that the gateway reaches both exporters:

```bash
podman exec gateway-secure curl -s -o /dev/null -w 'node %{http_code}\n' "http://${NODE_EXPORTER_UPSTREAM}/metrics"
podman exec gateway-secure curl -s -o /dev/null -w 'gpu %{http_code}\n' "http://${GPU_EXPORTER_UPSTREAM}/metrics"
```

Walk through [nginx.secure.conf](../model-host/gateway/nginx.secure.conf): `ssl_verify_client
optional`, the CN map to an identity, the inference and metrics allowlists, `limit_req` and `limit_conn`
keyed on the identity, 256 KB bodies, everything else 404, and an access log with identity and serial but
no bodies or credentials. `diff model-host/gateway/nginx.conf model-host/gateway/nginx.secure.conf` shows
what module 6 added.

### 4. Desktop: certificates for each service

From the repository root (WSL):

```bash
scp <gpu-host>:<repo>/model-host/secrets/pki/root_ca.crt secrets/pki/root_ca.crt
scp <gpu-host>:<repo>/model-host/secrets/pki/password    secrets/pki/provisioner_password
chmod +x scripts/*.sh
scripts/issue-certs.sh
scripts/issue-certs.sh --show
```

`issue-certs.sh` creates a key on the desktop for `complaint-api`, `tools`, `prometheus`, and the
desktop `app-gateway`, and asks step-ca to sign each for one year. The private keys never leave the
desktop; the provisioner password does let this desktop issue any name (a demo shortcut).

### 5. Desktop: probe the gateway

Uncomment the module 6 block in `tools/.env` (set `MODEL_HOST`), then from `tools/`:

```bash
podman-compose run --rm gateway-check
```

Expected (all PASS): 8080 closed; `/health` and `/version` without a certificate 200; `/v1/models`
without a certificate 401; a self-signed `tools` certificate 401; the `tools` certificate 200 on
inference, 403 on `/metrics` and `/metrics/node`, 404 on `/v1/completions` and `/tokenize`, 413 on a
300 KB body, some 429 (with `Retry-After`) in a burst of 120; no course root CA: TLS error. The report is
in `results/security/`. On the GPU host, `podman logs gateway-secure | tail` shows `"identity":"tools"`
and the certificate serial on each line.

```bash
podman-compose run --rm smoke --skip-structured     # module 1 smoke test, now over TLS as `tools`
```

Talking points: the gateway authenticates **services**, not people; rate limits count requests per
identity, not tokens (a token quota needs the response, which NGINX does not read); body limits and the
allowlist reduce what reaches vLLM, but neither stops prompt injection.

### 6. Desktop: the application over TLS

Uncomment the inference lines of the module 6 block in `app/.env` (leave `APP_AUTH_MODE` for step 9), then
from `app/`:

```bash
podman-compose up -d --force-recreate complaint-api
curl -s http://127.0.0.1:8090/healthz | jq .dependencies.inference
podman-compose run --rm cli submit A01
```

`inference.status_code` is 200 and the case reaches `pending_review`. The gateway log shows
`"identity":"complaint-api"`.

### 7. Desktop: Prometheus on the authenticated metrics route

In `observability/.env` set `PROMETHEUS_CONFIG=prometheus.secure.yml`, then from `observability/`:

```bash
podman-compose up -d --force-recreate prometheus
```

In Prometheus (`http://127.0.0.1:9090/targets`), `vllm`, `node`, and `gpu` are UP with
`https://slm.taco.internal:8443/metrics...`; the job and instance labels are unchanged, so the Model
serving and Model host dashboards continue the same series with a gap of one or two scrape intervals at
the switch. Show the rejected paths from the desktop:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://<MODEL_HOST>:8080/metrics      # connection refused (000)
curl -s -o /dev/null -w '%{http_code}\n' --cacert ../secrets/ca/root_ca.crt \
  --resolve slm.taco.internal:8443:<MODEL_HOST> https://slm.taco.internal:8443/metrics     # 401
curl -s -o /dev/null -w '%{http_code}\n' --cacert ../secrets/ca/root_ca.crt \
  --cert ../secrets/certs/complaint-api/tls.crt --key ../secrets/certs/complaint-api/tls.key \
  --resolve slm.taco.internal:8443:<MODEL_HOST> https://slm.taco.internal:8443/metrics     # 403: inference identity
```

### 8. Rotate a service credential

Desktop, repository root:

```bash
scripts/issue-certs.sh --rotate complaint-api
cd app && podman-compose run --rm cli reset CASE-10849 && podman-compose run --rm cli submit A02 && cd ..   # A02 is CASE-10849
```

The complaint API picks up the new key and certificate on its next connection (no restart); the old pair
is kept in `secrets/rotated/` and step-ca no longer renews it. The old certificate is still valid for a
year, so the rotation window ends when the gateway denies its serial. Wait a minute connections recycle
after 30 s idle or 100 requests.

On the **GPU host** run the command the script printed:

```bash
./model-host/pki/deny-serial.sh <OLD-SERIAL> complaint-api-rotation
```

Desktop, from `tools/`:

```bash
podman-compose run --rm gateway-check \
  --extra-cert /run/taco-rotated/complaint-api-<stamp>.crt /run/taco-rotated/complaint-api-<stamp>.key 401
```

The old certificate now gets 401 while the application keeps working on the new one.

### 9. Application-level authorization

Create the application secrets and switch the app to Authelia (desktop, repository root):

```bash
scripts/setup-app-auth.sh
```

In `app/.env` uncomment `APP_AUTH_MODE=authelia` (and `TRACE_CONTENT_POLICY`), and in `tools/.env`
uncomment `APP_AUTH_MODE=authelia`. Then from `app/`:

```bash
podman-compose up -d authelia app-gateway
podman-compose up -d --force-recreate case-api complaint-api
podman-compose run --rm cli reset CASE-10431 CASE-10431-J03
```

Now every call goes through `https://app.taco.internal:8443` with the user's password; the CLI sends HTTP
Basic for the actor it runs as. Requests that skip the gateway are refused even with forged headers:

```bash
curl -s -H 'Remote-User: sup-area-01' -H 'Remote-Groups: area_manager' http://127.0.0.1:8090/cases   # 401
```

The denials, each decided in application code from the trusted identity and `actors.json`:

```bash
podman-compose run --rm cli --actor rep-wg-01 submit A01        # 404: KOP case, WG rep (before any model call)
podman-compose run --rm cli submit A01                          # rep-kop-01: pending_review
podman-compose run --rm cli --actor sup-wg-01 approve CASE-10431  # 404: not in sup-wg-01's stores
podman-compose run --rm cli --actor qa-01 approve CASE-10431      # 403: quality reviewers cannot approve
podman-compose run --rm cli --actor rep-kop-01 reset CASE-10431   # 403 from Authelia: admin group only
podman-compose run --rm cli submit J03 --steps                  # injection asks for TA-10016
```

In J03 the complaint asks the agent to look up and refund another customer's order. Whatever the model
does, `get_order` runs with the case's customer and the actor's stores from trusted context, so the
other order is not found and the proposal can only cover TA-10431. The browser path: open
`https://app.taco.internal:8443/ui`, sign in as `sup-kop-01` with the password from
`secrets/users/password`, and approve CASE-10431; the banner shows the Authelia user and the `?actor=`
picker is gone.

Talking points: separating the hosts and the gateways is not high availability (each is one instance);
network membership is not authorization (the shared gateway secret is what the API checks); the
provisioner password on the desktop and readable key files are demo shortcuts.

# Module 6: Securing and Auditing the Deployment

Runbooks for Demos 6.1 and 6.2. Module 6 puts the model behind a TLS gateway that knows each calling
service by its client certificate, moves Prometheus to an authenticated metrics route, signs people in
to the complaint API through Authelia, and records an encrypted, tamper-evident audit trail that is
separate from operational logs and quality samples.

| Demo | File | Outcome |
| --- | --- | --- |
| 6.1 | [demo-6.1-secure-the-request-path.md](demo-6.1-secure-the-request-path.md) | The course CA (step-ca), the TLS gateway with per-service client certificates, a route allowlist, size and rate limits; Prometheus on the authenticated metrics route with continuous history; a certificate rotation; store-scoped authorization in the application |
| 6.2 | [demo-6.2-audit-a-resolution.md](demo-6.2-audit-a-resolution.md) | A signed-in request from intake to recorded action, reconstructed from the audit trail; content-free operational logs; encrypted content readable only by the auditor; the hash chain; a case deletion request; redacted traces; stop vs reset |

## Components used

| Folder | Used for |
| --- | --- |
| [model-host/](../model-host/README.md) | `step-ca` and `gateway-secure` services, [gateway/nginx.secure.conf](../model-host/gateway/nginx.secure.conf), [pki/init-ca.sh](../model-host/pki/init-ca.sh), [pki/deny-serial.sh](../model-host/pki/deny-serial.sh), exporters moved off the LAN (`local.env`) |
| [app/](../app/README.md) | `APP_AUTH_MODE=authelia`, the `authelia` and `app-gateway` services ([authelia/](../app/authelia/configuration.yml), [gateway/](../app/gateway/nginx.conf)), audit capture and `/audit` routes, the `taco-app-audit` volume, `TRACE_CONTENT_POLICY`, the `reset` one-shot, CLI `audit` commands, actors `aud-01` and `admin-01` |
| [tools/](../tools/README.md) | `gateway-check` (the expected-response matrix); every inference client and harness uses the client certificate and app-gateway when configured |
| [observability/](../observability/README.md) | [prometheus.secure.yml](../observability/prometheus/prometheus.secure.yml) selected by `PROMETHEUS_CONFIG` |
| [scripts/](../scripts/) | `issue-certs.sh` (desktop certificates and rotation), `setup-app-auth.sh` (Authelia users, demo password, audit key, gateway secret) |
| [shared/](../shared/README.md) | `taco_shared/tls.py`, `taco_shared/audit/`, `taco_shared/tracing.py` |
| [secrets/](../secrets/README.md) | what each module 6 file holds and who reads it |

## Identities

| Who | Proves it with | Checked by | Decides |
| --- | --- | --- | --- |
| `complaint-api`, `tools` (services) | client certificate (CN) from the course CA | `gateway-secure` (GPU host) | inference routes only, per-identity rate limits |
| `prometheus` (service) | client certificate | `gateway-secure` | metrics routes only |
| People (`rep-kop-01`, `sup-kop-01`, `aud-01`, ...) | Authelia session (browser) or HTTP Basic (CLI, harnesses) | Authelia via `app-gateway` (desktop) | role from the Authelia group; store scope from `actors.json` |
| `admin-01` | HTTP Basic | Authelia | the only identity allowed on `/admin` and `/case-admin` |

## Data paths

| Path | Contents | Where | Who reads |
| --- | --- | --- | --- |
| Operational telemetry | ids, versions, timings, counts, error kinds; no content | JSON logs, Prometheus, LangSmith (`TRACE_CONTENT_POLICY`) | operators |
| Quality evaluation | sampled complaint, evidence, proposal | `taco-app-quality` | `quality_reviewer`, `area_manager` |
| Audit | who requested, accessed, approved, executed; encrypted content | `taco-app-audit` (`audit.db`) | `auditor` (content), `area_manager` (metadata, own stores) |

## Setup (once)

**GPU host** (repository pulled, `source model-host/env.sh`): Demo 6.1 steps 1-3 create the CA and
start the secure gateway.

**Desktop** (WSL): the Python 3 and Podman already used by the course. For browser logins, add to
`C:\Windows\System32\drivers\etc\hosts` (as administrator):

```text
127.0.0.1  app.taco.internal auth.taco.internal
```

and import `secrets/ca/root_ca.crt` into **Trusted Root Certification Authorities** for the current user
(`certmgr.msc`, or double-click the file) after Demo 6.1 step 4. Remove both after the course.

Rebuild both images once (new dependency: `cryptography`):

```bash
cd app && APP_VERSION=$(git rev-parse --short HEAD) podman-compose build
cd ../tools && podman-compose build
```

## Returning to modules 1-5

Comment out the module 6 blocks in `app/.env`, `tools/.env`, and `observability/.env`, recreate
`complaint-api`, `case-api`, and `prometheus`, and on the GPU host `podman compose stop gateway-secure &&
podman compose up -d gateway` (remove `model-host/local.env` and recreate the exporters to put them back
on the LAN). Certificates, the CA, Authelia's data, and the audit store are kept.

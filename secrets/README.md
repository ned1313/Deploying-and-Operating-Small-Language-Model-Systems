# Secrets

Credential files for every compose project on this machine. They are mounted into containers at
runtime and are git-ignored (only this README is committed). They are plain files, not an encrypted
secret store. Each host has its own checkout and its own `secrets/`.

| File | Read by | Create with |
| --- | --- | --- |
| `inference_api_key` | `app/` (`complaint-api`, `basic-agent`), `tools/` (`agent-eval`, `load`) | `printf 'demo-placeholder-key' > secrets/inference_api_key` |
| `grafana_admin_password` | `observability/` (`grafana`) | `printf '%s' "$(openssl rand -hex 16)" > secrets/grafana_admin_password` |

On Windows PowerShell: `Set-Content -NoNewline secrets/inference_api_key 'demo-placeholder-key'`.

The inference key is sent as a bearer token but never checked: module 6 authenticates services with
client certificates instead (below). It is not the identity of the person using the complaint API; that
is the `X-Actor-Id` header (modules 2-5) or the Authelia user (module 6). The
services never print or log the value. LangSmith's API key is the exception to this folder: it is set
in the git-ignored `app/.env`.

## Module 6

Module 6 replaces the inference key with client certificates from the course CA (step-ca on the GPU
host) and adds Authelia for people. Each folder below is committed empty (`.gitkeep`) so the compose
mounts work in modules 1-5; the scripts fill them.

| Folder / file | Holds | Read by | Created by |
| --- | --- | --- | --- |
| `pki/root_ca.crt`, `pki/provisioner_password` | course root certificate; step-ca provisioner password | `scripts/issue-certs.sh` only | copied from the GPU host (`model-host/secrets/pki/`) |
| `ca/root_ca.crt` | root certificate (public) | `cli`, harnesses (verify app-gateway) | `scripts/issue-certs.sh` |
| `certs/<identity>/tls.crt, tls.key, root_ca.crt` | one-year certificate per identity: `complaint-api`, `tools`, `prometheus` (clients), `app-gateway` (server) | the matching service only | `scripts/issue-certs.sh` |
| `rotated/` | certificates replaced by `issue-certs.sh --rotate` | `gateway-check --extra-cert` | `scripts/issue-certs.sh` |
| `authelia/` | Authelia session, storage, and JWT secrets; `users_database.yml` (argon2 hashes) | `authelia` | `scripts/setup-app-auth.sh` |
| `users/password` | the single demo password for every synthetic user | `cli`, harnesses, Locust | `scripts/setup-app-auth.sh` |
| `gateway/shared_secret` | proves a request came through app-gateway | `app-gateway`, `complaint-api`, `case-api` | `scripts/setup-app-auth.sh` |
| `audit/audit_content_key` | Fernet key for captured audit content; audit is on while it exists | `complaint-api` | `scripts/setup-app-auth.sh` |

Certificate folders are mode 0711 with 0644 files so non-root container users (Prometheus) can read
them; the provisioner password lets this desktop issue any identity. Both are demonstration trade-offs
(planning decisions 55 and 63), discussed in the course rather than recommended.

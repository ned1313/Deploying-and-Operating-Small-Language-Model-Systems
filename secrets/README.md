# Secrets

Credential files for every compose project on this machine. They are mounted into containers at
runtime and are git-ignored (only this README is committed). They are plain files, not an encrypted
secret store. Each host has its own checkout and its own `secrets/`.

| File | Read by | Create with |
| --- | --- | --- |
| `inference_api_key` | `app/` (`complaint-api`, `basic-agent`), `tools/` (`agent-eval`, `load`) | `printf 'demo-placeholder-key' > secrets/inference_api_key` |
| `grafana_admin_password` | `observability/` (`grafana`) | `printf '%s' "$(openssl rand -hex 16)" > secrets/grafana_admin_password` |

On Windows PowerShell: `Set-Content -NoNewline secrets/inference_api_key 'demo-placeholder-key'`.

The inference key authenticates the application to the inference gateway (module 6 enforces it). It
is not the identity of the person using the complaint API; that is the `X-Actor-Id` header. The
services never print or log the value. LangSmith's API key is the exception to this folder: it is set
in the git-ignored `app/.env`.

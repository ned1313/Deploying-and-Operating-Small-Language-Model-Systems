# m4/secrets

Git-ignored. Create before starting Grafana:

```bash
printf '%s' "$(openssl rand -hex 16)" > secrets/grafana_admin_password
```

Grafana reads it through `GF_SECURITY_ADMIN_PASSWORD__FILE` on first start (log in as `admin`). Anonymous
visitors get read-only access, and Grafana is published on loopback only. Changing the file later does
not change the stored password; use `podman exec taco-observe_grafana_1 grafana cli admin reset-admin-password ...`.

The load service reuses the inference key from `../m3/secrets/inference_api_key`. LangSmith settings live
in `m4/.env`, which is also git-ignored.

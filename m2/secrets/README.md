# Secrets

Files in this directory are mounted into containers at runtime and are git-ignored (only this README
is committed). They are plain files, not an encrypted secret store.

Create the inference service credential before the first `podman-compose run`:

```bash
printf 'demo-placeholder-key' > secrets/inference_api_key
```

```powershell
Set-Content -NoNewline -Path secrets/inference_api_key -Value 'demo-placeholder-key'
```

The module 1 gateway does not check the key yet; module 6 starts enforcing it with no code change.
The scripts never print or log the value.

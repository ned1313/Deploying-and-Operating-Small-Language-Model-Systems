# Secrets

Files in this directory are mounted into containers at runtime and are git-ignored (only this README
is committed). They are plain files, not an encrypted secret store.

Module 3 uses the same inference service credential as module 2. Copy it, or create it:

```bash
cp ../m2/secrets/inference_api_key secrets/inference_api_key
# or
printf 'demo-placeholder-key' > secrets/inference_api_key
```

```powershell
Copy-Item ../m2/secrets/inference_api_key secrets/inference_api_key
```

This key authenticates the application to the inference gateway (module 6 enforces it). It is not
the identity of the person using the complaint API; that is the `X-Actor-Id` header. The services
never print or log the value.

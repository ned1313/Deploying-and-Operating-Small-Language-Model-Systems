# Demo 5.2: Release and roll back a model version

Goal: pin the baseline and a candidate as release manifests, replace the model on the single GPU in a
maintenance window, evaluate the candidate with the same workload and network path, reject it on the
release gates, roll back with one command, and verify recovery.

Prerequisites: the module 5 setup in [README.md](README.md): the GPU host serves `r1-qwen3-14b` through
`release.sh` (alias `taco-slm`), and the desktop uses `INFERENCE_MODEL=taco-slm`. Desktop commands run
from `tools/` unless noted; **GPU host** commands run from the repository root with
`source model-host/env.sh`.

The candidate is Llama-3.1-8B (w8a8): smaller and faster on the same card. The expected result is a
quality regression that the gates reject; the numbers below are filled in from your run.

---

## Clip 3: testing model versions and updates

### 1. What a release pins

```bash
cat model-host/releases/r1-qwen3-14b.json
diff <(jq -S . model-host/releases/r1-qwen3-14b.json) <(jq -S . model-host/releases/r2-llama-3.1-8b.json)
grep -E '^export (VLLM_IMAGE|SERVED_ALIAS|QWEN_REVISION|LLAMA_REVISION)' model-host/env.sh
cat tools/quality-gates.yaml
```

Key points:

- The model repo **and commit**, the quantization and parsers, the vLLM image tag **and its digests**,
  the serving settings, and the app-side inference settings are all in the manifest. The prompt, policy,
  schema, and dataset versions are recorded with it; `taco_build_info` reports the same fields at runtime.
- Both releases are served as `taco-slm` plus their own name. The application only knows `taco-slm`, so a
  release or rollback is a model-host action; the app reads the real model from `/v1/models`.
- The gates are decided before the candidate runs: e2e pass rate and policy compliance within 5 points of
  the baseline (and at least 0.75), grounded/schema/duplicate-free at 100%, all failure tests, workflow
  p95 within 120% of the baseline under steady load, no errors, peak VRAM under 23 GiB.

### 2. Record the baseline's evidence

Run these while the baseline release is serving. Each release's runs carry the release id as their
label, so the gate report can find them:

```bash
podman-compose run --rm e2e-eval --label r1-qwen3-14b
podman-compose run --rm failure-tests --label r1-qwen3-14b
podman-compose run --rm load --mode app --shape steady --duration 300 --label r1-qwen3-14b
podman-compose run --rm baseline --label r1-qwen3-14b
```

### 3. Apply the candidate in a maintenance window

**GPU host**:

```bash
./model-host/release.sh apply r2-llama-3.1-8b
```

`release.sh` recreates `vllm` with the manifest's settings (Qwen stops, Llama loads), waits until
`llama-3.1-8b` is served, checks the image digest, runs the smoke test from a tools container, and records
`current: r2-llama-3.1-8b, previous: r1-qwen3-14b` in `results/serving/release-state.json`. It ends with
"Desktop: nothing to change" because both manifests use the same app settings.

**Desktop**, while the model loads (from `app/`):

```bash
cd ../app
podman-compose run --rm cli reset CASE-10431
podman-compose run --rm cli submit A01        # failed_retryable (inference_unavailable)
```

After `release.sh` finishes:

```bash
podman-compose run --rm cli retry CASE-10431
curl -s http://127.0.0.1:8090/healthz | jq -c '{model, alias}'     # {"model":"llama-3.1-8b","alias":"taco-slm"}
```

The case resumes from its checkpoint and reaches review on the new model. Nothing in the application was
restarted; within 30 s Grafana's deployment-versions table shows `llama-3.1-8b`.

### 4. Evaluate the candidate on the same workload and path

```bash
cd ../tools
podman-compose run --rm e2e-eval --label r2-llama-3.1-8b
podman-compose run --rm failure-tests --label r2-llama-3.1-8b
podman-compose run --rm load --mode app --shape steady --duration 300 --label r2-llama-3.1-8b
podman-compose run --rm baseline --label r2-llama-3.1-8b
podman-compose run --rm quality score --label r2-llama-3.1-8b
```

This takes about 15 minutes. The quality dashboard shows the two models as separate series.

### 5. The release gate

```bash
podman-compose run --rm release --baseline r1-qwen3-14b --candidate r2-llama-3.1-8b
```

The report (`results/release/release-r2-llama-3.1-8b-vs-r1-qwen3-14b-<ts>.md`) lists each gate with the
candidate's value, the threshold, and pass/FAIL, then the two releases side by side and the scenarios
each one failed. It exits non-zero on REJECT, so it can block a pipeline.

Takeaways: a faster model is not a better release; the decision is made on the gates agreed in
advance; 24 scenarios give wide error bars, so a one-scenario margin is a reason to rerun, not proof.

### 6. Roll back

**GPU host**:

```bash
./model-host/release.sh rollback
./model-host/release.sh status
```

Rollback re-applies the previous manifest through the same path (load, digest check, smoke test) and
swaps current and previous.

### 7. Verify recovery

```bash
podman-compose run --rm smoke --skip-structured --model qwen3-14b \
    --extra-body '{"chat_template_kwargs": {"enable_thinking": false}}'
podman-compose run --rm e2e-eval --label r1-qwen3-14b-rollback          # all 24
(cd ../app && podman-compose run --rm cli reset CASE-10431 && podman-compose run --rm cli submit A01)
```

The e2e result should match the baseline within run-to-run variation (S09 and S15 fail as before). In
Grafana, the deployment-versions table and the quality series show `qwen3-14b` again.

---

## Notes

- One GPU means sequential replacement: there is no blue/green and no traffic split. The outage lasts
  as long as the model load (minutes); `release.sh` prints the window and the cases it affects end
  `failed_retryable` with their checkpoints, ready for `retry`.
- If a candidate needs different app settings (`INFERENCE_EXTRA_BODY`), `release.sh` prints the desktop
  step instead of "nothing to change".
- If the smoke test fails, `release.sh` exits non-zero and suggests `rollback`; the state file records
  `smoke_passed: false`.

# Demo 1.3: Tune serving under load

Goal: drive direct-inference traffic at the selected model while varying concurrency, context
limits, KV-cache allocation, and batching controls. Show the throughput/latency tradeoff, a
memory-pressure case, and a hard out-of-memory failure. Finish with a baseline configuration that
has measured headroom.

Prerequisites: Demo 1.2 state (selected candidate running via `serve.sh`, `gateway` up,
`source m1/env.sh`). The examples below use Qwen; substitute the winner from Demo 1.2.

```bash
CAND=qwen                       # qwen | llama | granite
SERVED="$QWEN_SERVED_NAME"      # LLAMA_SERVED_NAME | GRANITE_SERVED_NAME
EXTRA="$QWEN_EXTRA_BODY"        # LLAMA_EXTRA_BODY  | GRANITE_EXTRA_BODY
```

---

## 1. Read the memory budget from the startup log

```bash
podman compose logs vllm 2>&1 | grep -E 'GPU KV cache size|Maximum concurrency|model weights take|non_torch_memory|Available KV cache memory'
```

Explain the arithmetic: `--gpu-memory-utilization` x 24 GB = total budget; minus weights; minus
activation/runtime reserve; the remainder becomes KV-cache blocks. "Maximum concurrency for N
tokens per request" is how many *full-length* requests fit at once. Real requests are shorter,
so real concurrency is higher, until it is not.

```bash
# The same numbers, live, as Prometheus series
curl -s "http://localhost:${GATEWAY_PORT}/metrics" | grep -E '^vllm:(num_requests_running|num_requests_waiting|gpu_cache_usage_perc|kv_cache_usage_perc|num_preemptions_total)'
```

---

## 2. Baseline sweep

Streamed requests give time-to-first-token (prefill) separately from total latency (decode).
The script samples `/metrics` every second to catch queueing and preemptions.

```bash
podman compose run --rm tools tools/load_sweep.py \
    --model "$SERVED" --extra-body "$EXTRA" \
    --concurrency 1,4,8,16,32 --requests-per-level 32 --max-tokens 300 \
    --label baseline
```

Narration while it runs (keep nvtop visible):

- concurrency 1 to 4: output tok/s climbs almost linearly, p95 barely moves. Decode is
  memory-bandwidth bound; batching more sequences is nearly free.
- around `--max-num-seqs` (32): `peak waiting` becomes non-zero. Requests queue at the scheduler;
  p95 grows with queue depth even though tok/s is flat. That flat line is the card's ceiling for
  this model, quantization, and output length.
- TTFT p95 growing faster than TTFT p50 = prefill of new arrivals is competing with decode of the
  running batch (chunked prefill controls that; see step 5).

---

## 3. Context pressure: longer prompts, same settings

Pad each prompt by ~3000 tokens (the agent in module 2 will carry tool results, so this is realistic).

```bash
podman compose run --rm tools tools/load_sweep.py \
    --model "$SERVED" --extra-body "$EXTRA" \
    --concurrency 8,16,32 --requests-per-level 32 --max-tokens 300 --pad-prompt-tokens 3000 \
    --label ctx-pressure
```

Look for `kv %` near 100 and `preempt` > 0: the scheduler is evicting sequences and recomputing
them later. Throughput drops and p95 spikes. In the vLLM log:

```bash
podman compose logs vllm 2>&1 | grep -iE 'preempt|recompute' | tail -5
```

---

## 4. Two ways to break it

### 4a. Startup failure: context length the KV cache cannot hold

```bash
./m1/serve.sh $CAND --max-model-len 131072 --gpu-memory-utilization 0.85
podman compose logs -f vllm    # ValueError: the model's max seq len is larger than the KV cache can store ...
```

The message includes the number of tokens that *do* fit and suggests lowering `--max-model-len`
or raising `--gpu-memory-utilization`. This is the best kind of failure: it happens before any
traffic.

### 4b. Runtime OOM: over-committing the card

Give vLLM nearly the whole card, then take some of it away with a second process.

```bash
./m1/serve.sh $CAND --gpu-memory-utilization 0.97
podman compose logs -f vllm    # wait for startup; nvtop shows ~23 GB in use

# In a second SSH session, occupy ~1.5 GB of VRAM alongside the server (reuses the vLLM image for torch):
podman run --rm --device nvidia.com/gpu=all --name vram-hog --entrypoint python3 "$VLLM_IMAGE" -c \
  'import torch,time; x=torch.empty(int(1.5*1024**3), dtype=torch.uint8, device="cuda"); print("holding 1.5 GiB"); time.sleep(600)'

# Back in the first session, push a burst through:
podman compose run --rm tools tools/load_sweep.py --model "$SERVED" --extra-body "$EXTRA" \
    --concurrency 32 --requests-per-level 64 --max-tokens 400 --pad-prompt-tokens 3000 --label oom
podman compose logs vllm 2>&1 | grep -iE 'out of memory|CUDA error|EngineCore' | tail -5
podman ps -a --filter name=vllm --format '{{.Status}}'
```

Depending on the vLLM version, the engine either dies (container exits, every request 5xx) or
survives with heavy preemption. Either way the lesson is the same: `--gpu-memory-utilization` is
a promise about *exclusive* use of the card. Clean up:

```bash
podman rm -f vram-hog 2>/dev/null
```

---

## 5. Knobs that trade memory for throughput or latency

Change one thing at a time and rerun the baseline sweep with a new `--label`.

```bash
# (a) Smaller context, more sequences: fits the screening workload, more KV blocks per request.
./m1/serve.sh $CAND --max-model-len 4096 --max-num-seqs 64
podman compose logs -f vllm
podman compose run --rm tools tools/load_sweep.py --model "$SERVED" --extra-body "$EXTRA" \
    --concurrency 8,16,32,64 --requests-per-level 64 --max-tokens 300 --label ctx4k-seq64

# (b) FP8 KV cache: roughly doubles KV capacity; check the "GPU KV cache size" log line.
./m1/serve.sh $CAND --kv-cache-dtype fp8
podman compose logs vllm 2>&1 | grep -E 'GPU KV cache size|Maximum concurrency'
podman compose run --rm tools tools/load_sweep.py --model "$SERVED" --extra-body "$EXTRA" \
    --concurrency 8,16,32 --requests-per-level 32 --max-tokens 300 --pad-prompt-tokens 3000 --label kv-fp8

# (c) Batching control: cap tokens scheduled per step. Lower = smoother TTFT for new arrivals,
#     lower peak throughput; higher = the opposite.
./m1/serve.sh $CAND --max-num-batched-tokens 2048
podman compose logs -f vllm
podman compose run --rm tools tools/load_sweep.py --model "$SERVED" --extra-body "$EXTRA" \
    --concurrency 8,16,32 --requests-per-level 32 --max-tokens 300 --label batched-2048

# (d) Guided decoding overhead under load (the application will use json_schema in module 2).
podman compose run --rm tools tools/load_sweep.py --model "$SERVED" --extra-body "$EXTRA" \
    --concurrency 8,16 --requests-per-level 32 --max-tokens 300 --structured-output json_schema --label guided
```

Compare runs side by side from the saved JSON:

```bash
python3 - "$RESULTS_DIR"/load-*.json <<'EOF'
import json, sys
print(f"{'run':<34}{'conc':>5}{'req/s':>8}{'tok/s':>8}{'p50':>7}{'p95':>7}{'ttft95':>8}{'wait':>6}{'kv%':>6}{'pre':>5}{'err':>5}")
for path in sys.argv[1:]:
    r = json.load(open(path))
    for lvl in r["levels"]:
        v, lat, t = lvl["vllm"], lvl["latency_seconds"], lvl["ttft_seconds"]
        f = lambda x, w=7, p=2: f"{x:>{w}.{p}f}" if isinstance(x, (int, float)) else f"{'-':>{w}}"
        print(f"{(r['label'] or 'run')[:33]:<34}{lvl['concurrency']:>5}{f(lvl['requests_per_second'],8)}{f(lvl['output_tokens_per_second'],8,0)}"
              f"{f(lat['p50'])}{f(lat['p95'])}{f(t['p95'],8)}{f(v['peak_waiting'],6,0)}{f(v['peak_kv_cache_usage_pct'],6,0)}{f(v['preemptions_delta'],5,0)}{lvl['errors']:>5}")
EOF
```

---

## 6. Pick the baseline and confirm quality did not move

Choose the configuration whose p95 stays under the target (course target: p95 < 8 s at 16
concurrent requests, no preemptions, KV usage under ~80% at that load). Then rerun the screening
once to confirm the serving change did not alter answers.

```bash
# Example baseline; write the real values into m1/env.sh so serve.sh reproduces them
export MAX_MODEL_LEN=4096 MAX_NUM_SEQS=64 GPU_MEMORY_UTILIZATION=0.90
./m1/serve.sh $CAND --kv-cache-dtype fp8
podman compose logs -f vllm

podman compose run --rm tools tools/screen_models.py --model "$SERVED" --extra-body "$EXTRA" \
    --label tuned-baseline

podman compose run --rm tools tools/compare_screening.py \
    "results/screening-${SERVED}-baseline-*.json" "results/screening-${SERVED}-tuned-baseline-*.json"

# Refresh the deployment record with the final arguments, taken from the running container
podman compose run --rm tools tools/smoke_test.py --model "$SERVED" --extra-body "$EXTRA" \
    --hf-repo "$QWEN_REPO" --revision "$QWEN_REVISION" --quantization "$QWEN_QUANT_LABEL" \
    --chat-template "tokenizer default" --reasoning-parser "$QWEN_REASONING_PARSER" \
    --tool-call-parser "$QWEN_TOOL_PARSER" --serving-image "$VLLM_IMAGE" \
    --serving-args "$(podman inspect vllm --format '{{join .Config.Cmd " "}}')" \
    --notes "module 1 tuned baseline"
```

Limitations to state on camera: 32-64 requests per level is enough to see the shape of the curve,
not to certify an SLO; the workload is synthetic and uniform; and quality was checked with a
20-scenario screen. Module 4 adds Locust, Prometheus, and Grafana for the real baseline.

Leave `vllm` and `gateway` running. Module 2 consumes `http://${MODEL_HOST}:${GATEWAY_PORT}/v1`
with model name `$SERVED`.

# Demo 1.3: Tune serving under load

Goal: drive direct-inference traffic at the selected model while varying concurrency, context
limits, KV-cache allocation, and batching controls. Show the throughput/latency tradeoff, a
memory-pressure case, and a hard out-of-memory failure. Finish with a baseline configuration that
has measured headroom.

Prerequisites: Demo 1.2 state (selected candidate running via `serve.sh`, `gateway` up,
`source m1/env.sh`). The examples below use Qwen; substitute the winner from Demo 1.2.

```bash
CAND=qwen                       # llama | qwen | granite
SERVED="$QWEN_SERVED_NAME"      #  LLAMA_SERVED_NAME | GRANITE_SERVED_NAME
EXTRA="$QWEN_EXTRA_BODY"        #  LLAMA_EXTRA_BODY | GRANITE_EXTRA_BODY
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

### What the flags control

| flag | meaning |
| --- | --- |
| `--concurrency 1,4,8,16,32` | One run ("level") per value. At each level the script keeps exactly N requests in flight: when one finishes, the next starts. This is a closed-loop test, so the load never exceeds N. |
| `--requests-per-level 32` | Requests to complete at each level before moving on. Prompts cycle through the 22 screening scenarios, so the prompt mix is the same at every level. |
| `--max-tokens 300` | Output budget per request. It caps decode time and how far each sequence can grow its KV-cache footprint. |
| `--pad-prompt-tokens N` | Appends about N tokens of filler text to each prompt. This raises prefill cost and KV-cache use per request (used in step 3). |
| `--structured-output json_schema` | Adds guided decoding, to measure its overhead under load (step 5d). |
| `--label` | Tag written into the results file name and summary, so runs can be compared later. |
| `--metrics-interval 1.0` | How often, in seconds, `/metrics` is scraped during a level. Spikes shorter than this can be missed. |

Requests use `temperature 0.7`, not 0. Identical greedy requests would all produce the same output
length, which makes the batch unrealistically uniform.

### What the output columns mean

Each level prints a progress line, and the run ends with a summary table. Both report the same
fields; the progress line spells some names out, e.g. `peak waiting`, `preemptions`, `errors`.
On older vLLM images, `kv %` is read from `vllm:gpu_cache_usage_perc` instead.

| column | measured by | what it tells you |
| --- | --- | --- |
| `conc` | client | The concurrency level for this row. |
| `req/s` | client | Successful requests divided by the level's wall-clock time. This is completed-work throughput. |
| `out tok/s` | client (`usage.completion_tokens`) | Generated tokens per second across *all* in-flight requests. This is decode throughput, the card's real output rate. It includes reasoning tokens if thinking is enabled. |
| `lat p50` / `lat p95` | client | Seconds from sending a request to receiving its last token. p50 is the typical request; p95 is the slow tail that users notice. Includes gateway and network time. |
| `ttft p50` / `ttft p95` | client (streaming) | Time to first token: seconds until the first streamed content arrives. This is roughly queue wait plus prefill. A rising TTFT with a flat `lat - ttft` means requests are waiting, not decoding slower. |
| `peak wait` | server (`vllm:num_requests_waiting`) | The most requests sitting in vLLM's queue at any scrape. Above 0 means the scheduler had no room to start them: `--max-num-seqs` or the KV cache is full. |
| `kv %` | server (`vllm:kv_cache_usage_perc`) | Peak share of preallocated KV-cache blocks in use. Near 100 means new tokens have nowhere to go. |
| `preempt` | server (`vllm:num_preemptions_total`, delta) | How many times vLLM evicted a running sequence during this level to free KV cache, recomputing it later. Any non-zero value is wasted GPU work and a latency spike. |
| `err` | client | Failed requests (HTTP errors or timeouts). The first three error messages are printed under the level. |

Rules of thumb for reading a sweep:

- **Spare capacity:** `out tok/s` rises with `conc` while `lat p95` stays roughly flat.
- **Saturation:** `out tok/s` flattens while `lat p95` and `ttft p95` keep climbing, and `peak wait` > 0. Adding users now only adds queueing.
- **Memory pressure:** `kv %` near 100 together with `preempt` > 0.

`peak_running`, token means, and the raw per-level numbers are also in
`results/load-<model>-<label>-<timestamp>.json` for later comparison (step 5).

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

# Try to occupy ~1.5 GB of VRAM alongside the server (reuses the vLLM image for torch):
podman run --rm --device nvidia.com/gpu=all --name vram-hog --entrypoint python3 "$VLLM_IMAGE" -c \
  'import torch,time; x=torch.empty(int(1.5*1024**3), dtype=torch.uint8, device="cuda"); print("holding 1.5 GiB"); time.sleep(600)'
```

The second container will fail to start since there is insufficient capacity: `--gpu-memory-utilization` is
a promise about *exclusive* use of the card.

Clean up:

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

Compare runs side by side from the saved JSON. The quotes keep the host shell from expanding the
glob; the tool expands it inside the container.

```bash
podman compose run --rm tools tools/compare_load.py 'results/load-*.json'

# Or pick specific runs and name the CSV
podman compose run --rm tools tools/compare_load.py --csv results/tuning.csv \
    'results/load-*-baseline-*.json' 'results/load-*-kv-fp8-*.json'
```

The tool prints two tables. **Runs** shows the settings for each sweep. **Levels** has one row per
concurrency level, with every column from step 2 plus `lat avg`/`lat max`, `ttft avg`/`ttft max`,
`run` (peak running), `in tok`/`out tok` (mean tokens per request), and `samples` (number of
`/metrics` scrapes).

It also writes `results/load-comparison-<timestamp>.csv` with one row per run and level and every
recorded field: run settings, the source file and timestamp, and the first error message. Copy it
to the desktop for Excel:

```bash
scp <user>@<MODEL_HOST>:<clone-path>/m1/results/load-comparison-*.csv .
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

# Refresh the deployment record with the final arguments (serve.sh saved them)
podman compose run --rm tools tools/smoke_test.py --model "$SERVED" --extra-body "$EXTRA" \
    --hf-repo "$QWEN_REPO" --revision "$QWEN_REVISION" --quantization "$QWEN_QUANT_LABEL" \
    --chat-template "tokenizer default" --reasoning-parser "$QWEN_REASONING_PARSER" \
    --tool-call-parser "$QWEN_TOOL_PARSER" --serving-image "$VLLM_IMAGE" \
    --serving-args "$(cat "$RESULTS_DIR/last-serving-args.txt")" \
    --notes "module 1 tuned baseline"
```

Limitations to state on camera: 32-64 requests per level is enough to see the shape of the curve,
not to certify an SLO; the workload is synthetic and uniform; and quality was checked with a
22-scenario screen. Module 4 adds Locust, Prometheus, and Grafana for the real baseline.

Leave `vllm` and `gateway` running. Module 2 consumes `http://${MODEL_HOST}:${GATEWAY_PORT}/v1`
with model name `$SERVED`.

# Demo 5.3: Profile and optimize cost

Goal: measure cost per successful resolution for a repeatable baseline workload, change one setting and
replay the same workload, accept the change that lowers the cost without hurting quality, and reject one
that lowers the cost of tokens but breaks resolutions.

Prerequisites: the module 5 setup in [README.md](README.md), the observability project running, and the
cost assumptions in `tools/.env` (the defaults are placeholders; every report prints the values it used).
Commands run from `tools/` unless noted. Every run uses `app` mode, the `load-v1` cases, seed 42, and the
same network path (desktop container -> complaint API -> LAN -> gateway -> vLLM).

---

## Clip 4: baselines and optimization (slides)

- **The unit of cost is a successful resolution**, not a request or a token: recorded (or correctly
  clarified), passing the deterministic checks and, for labeled synthetic cases, the label. A cheap wrong
  refund is not a saving.
- **Cost per hour is mostly fixed.** On-premises it is the amortized host plus power plus a share of the
  desktop; in the cloud it is the instance price. The module 4 baseline ran the GPU at 77% utilization with
  vLLM never running more than 2 sequences: most of each hour bought capacity the workload did not use.
- **Levers**: concurrency and batching (more resolutions per hour from the same hardware), context and
  output budgets (fewer tokens per run), and the model tier (Demo 5.2 measured what a smaller model costs
  in quality).
- **Faster or busier is not better by itself.** Every change is replayed against the same workload and
  accepted only if quality holds.

```
cost per successful resolution = runtime hours x hourly cost / successful resolutions
```

## Clip 5: load generation and optimizing settings

### 1. Baseline run

```bash
podman-compose run --rm load --mode app --shape steady --duration 300 --label cost-baseline
podman-compose run --rm cost cost-baseline
```

The cost tool scores any new samples, finds the run's window in `results/load/`, queries Prometheus for
GPU power and utilization and tokens over that window, and counts the successful resolutions among the
samples created in it. Output: `results/cost/cost-cost-baseline-<ts>.md`.

### 2. Accepted change: more concurrency

Raise the application's run slots and offer more users (from `app/`):

```bash
cd ../app
sed -i 's/^WORKFLOW_MAX_CONCURRENT_RUNS=.*/WORKFLOW_MAX_CONCURRENT_RUNS=8/' .env
podman-compose up -d --force-recreate complaint-api
cd ../tools
podman-compose run --rm load --mode app --shape steady --users 6 --duration 300 --label cost-concurrency
podman-compose run --rm cost cost-baseline cost-concurrency
```

Read the side-by-side table: successful resolutions per hour, vLLM max running sequences, GPU utilization
and power, workflow p95, and the cost per successful resolution under both assumption sets. Then the
quality gate for the change:

```bash
podman-compose run --rm quality report --label cost-concurrency --since <started_at from the cost report>
```

Accept when the failed-check and failed-label counts stay in line with the baseline and the latency is
still acceptable (workflow p95 under the 20 s alert threshold). Watch **Model serving** during the run:
running sequences rise with the users, waiting stays at 0, and the KV cache has room.

### 3. Rejected change: a smaller output budget

`AGENT_MAX_OUTPUT_TOKENS` caps every model call. The proposal call needs about 400 output tokens; cap it
below that (from `app/`):

```bash
cd ../app
sed -i 's/^AGENT_MAX_OUTPUT_TOKENS=.*/AGENT_MAX_OUTPUT_TOKENS=256/' .env
podman-compose up -d --force-recreate complaint-api
cd ../tools
podman-compose run --rm load --mode app --shape steady --users 6 --duration 300 --label cost-short-output
podman-compose run --rm cost cost-concurrency cost-short-output
```

Tokens per run fall, but truncated proposals fail to parse, take a correction attempt, and escalate
(`validation_failed`): successful resolutions per hour drop and the cost per successful resolution rises.
**Complaint workflow** shows the escalations; **Quality and cost** shows the successful share falling.
Revert:

```bash
cd ../app
sed -i 's/^AGENT_MAX_OUTPUT_TOKENS=.*/AGENT_MAX_OUTPUT_TOKENS=800/' .env
podman-compose up -d --force-recreate complaint-api
```

### 4. Model tier

Use the Demo 5.2 runs (`cost r1-qwen3-14b r2-llama-3.1-8b`): the smaller model finishes each run faster,
but if it fails the release gates its cheaper hour buys fewer successful resolutions.

### 5. Limits of this evaluation

Five-minute runs on 150 synthetic cases show the direction and the size of an effect, not a certified
cost. The hourly costs are assumptions; the report prints them so they can be argued with. Samples still
waiting for approval when a run ends are not counted, which slightly favors shorter approval waits.

## Wrap-up state

`WORKFLOW_MAX_CONCURRENT_RUNS=8` if the concurrency change was accepted, `AGENT_MAX_OUTPUT_TOKENS=800`;
the GPU host on `r1-qwen3-14b`.

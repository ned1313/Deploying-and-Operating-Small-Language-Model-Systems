# Demo 4.1: Instrument and load the application

Goal: collect metrics from both hosts, drive normal and burst traffic, and read the signals that
separate the model server from the application: inference latency, tool latency, agent processing
time, and approval wait. Finish with a documented baseline.

Prerequisites: module 1 model and gateway running on the GPU host; the module 3 application running
from `m3/`; the module 4 setup in [README.md](README.md) on both hosts. Commands run from `m4/` on the
desktop unless marked **GPU host**.

Sample output below comes from the test run on 2026-10-12 (Qwen3-14B AWQ, RTX 3090,
`max-num-seqs` 64 on the running server, `WORKFLOW_MAX_CONCURRENT_RUNS=4`). Your numbers will differ.

---

## Clip 1: observability for agents and SLMs (slides)

Use the "Where each signal comes from" table and diagram in [README.md](README.md). Key points:

- **Metrics** answer "how much and how fast" (Prometheus); **logs** answer "what happened to this
  request" (JSON lines with `request_id`); **traces** answer "where did the time go inside this run"
  (LangSmith); **quality evaluations** answer "was the answer right" (module 5).
- Four latencies, each measured where it happens: client (Locust), application (`taco_http_*`,
  `taco_workflow_run_seconds`), application's view of the model (`taco_model_seconds`), and the server
  (`vllm:e2e_request_latency_seconds`). The gaps between them are network, gateway, tools, and queueing.
- Approval wait (`taco_approval_wait_seconds`) is human time. It is never part of processing latency.
- Thresholds live in `prometheus/rules/taco.yml`; the baseline at the end of this demo is what they
  are tuned against.

---

## Clip 2: collecting metrics from the model and hardware

### 1. Model-host exporters

**GPU host** (already running after setup):

```bash
podman ps --format '{{.Names}} {{.Status}}' | grep -E 'gateway|vllm|exporter'
```

From the desktop, prove the desktop can read them:

```bash
source <(grep -E '^MODEL_HOST=' .env)
curl -s "http://$MODEL_HOST:9400/metrics" | grep -E '^DCGM_FI_DEV_(GPU_UTIL|FB_USED)'
curl -s "http://$MODEL_HOST:8080/metrics" | grep -E '^vllm:(num_requests_running|kv_cache_usage_perc)'
```

Talking point: vLLM's metrics come through the gateway without authentication. That is the gap
module 6 closes; the job and instance labels (`job="vllm"`, `instance="model-host"`) stay the same so
dashboards survive the change.

### 2. Start the observability project and check the targets

```bash
podman-compose up -d prometheus grafana
curl -s http://127.0.0.1:9090/api/v1/targets | python3 -c '
import json, sys
for t in json.load(sys.stdin)["data"]["activeTargets"]:
    print(f"{t[\"labels\"][\"job\"]:<14} {t[\"labels\"][\"instance\"]:<14} {t[\"health\"]:<5} {t[\"scrapeUrl\"]}")'
```

```
complaint-api  complaint-api  up    http://complaint-api:8090/metrics
gpu            model-host     up    http://192.168.1.251:9400/metrics
node           model-host     up    http://192.168.1.251:9100/metrics
prometheus     prometheus     up    http://localhost:9090/metrics
vllm           model-host     up    http://192.168.1.251:8080/metrics
```

Or show **Status > Target health** in the Prometheus UI. Point out `complaint-api:8090`: Prometheus
reaches the API by name over the shared `taco-telemetry` network; the case API and the business
routes are not on that network.

### 3. Dashboards at idle

Open Grafana (`http://127.0.0.1:3000`), folder **Taco Alley**:

- **Model host**: VRAM is already above 90% with no traffic, because vLLM preallocates memory for the
  weights and the KV cache at startup. VRAM is not a load signal for vLLM; KV-cache usage is.
- **Model serving (vLLM)**: zero running and waiting, KV cache near 0%.
- **Complaint workflow**: the deployment-versions table at the bottom (app version, model, policy,
  prompt hash, schema, dataset).

### 4. Saturate the model server directly

```bash
podman-compose run --rm load --mode direct --shape burst --users 4 --burst-users 96 --label direct-burst
```

Four minutes: 4 users, then 96 for the middle two minutes, then 4. Each request is a module 1
screening prompt with the facts inlined and a `json_schema` response format. Watch **Model serving**:

- *Requests running* climbs to the server's `max-num-seqs` (64 here) and stops; *waiting* takes the rest (32).
- *Queue time* p95 rises from near zero to several seconds; *end-to-end latency* rises with it.
- *KV-cache usage* stays low (about 13%): the scheduler's sequence limit, not memory, is the bottleneck.
- *Tokens per second* plateaus: more users stopped buying throughput and only added latency.

Sample (120 s variant):

```
| chat_completions | 948 | 0 | 8.40 s | 11 s | 8.21 s | 7.35 |      (Locust: count, failures, median, p95, mean, req/s)
| vllm.e2e_p95_s | 13.9 s |
| vllm.queue_p95_s | 4.88 s |
| vllm.max_running | 64 |
| vllm.max_waiting | 32 |
| vllm.max_kv_cache | 13.1% |
```

### 5. Load the application end to end

```bash
podman-compose run --rm load --mode app --shape burst --label app-burst
```

2 users, 8 during the burst, 2 again; each user submits a different `load-v1` complaint as that
store's rep, and the synthetic reviewer `load-reviewer-01` approves accepted proposals 5-15 s later.
The run resets its 150 cases first. Watch **Complaint workflow** next to **Model serving**:

- *Runs in progress* pins at 4 (`WORKFLOW_MAX_CONCURRENT_RUNS`) and *queue wait* climbs: the
  application's run slots are the bottleneck.
- vLLM never queues (waiting 0, KV cache about 6%): the model has headroom the application does not use.
- *Node latency*: `propose` and `investigate` (model calls) dominate; `tools` is milliseconds.
- *Model call latency: application vs server*: the application's view of a model call is close to
  vLLM's own e2e time; the gap is network and gateway.

```
POST /complaints                68  0 | 17923 avg  20000 med  ... 95%: 26000 ms
POST /reviews/[case]/decision   66  0 |  7768 avg
outcomes: {'after_approval:recorded': 66, 'pending_review': 68}
```

Client p95 (26 s) vs workflow run p95 (14.6 s) vs vLLM e2e p95 (8 s): the client number includes the
queue wait for a run slot (p95 18.7 s). The approval calls queue behind submissions for the same slots,
which is why a fast `record` step shows seconds of latency here.

Talking point: the fix depends on which queue is long. A longer app queue with an idle model calls
for more run slots (or more workers); a long vLLM queue with high KV cache calls for a bigger GPU, a
smaller model, or shorter contexts. Module 5 changes one setting and measures it.

### 6. Monitoring is optional for the application

```bash
podman-compose stop prometheus grafana
(cd ../m3 && podman-compose run --rm cli reset CASE-10431 && podman-compose run --rm cli submit A01)
podman-compose up -d prometheus grafana
```

The complaint is processed with monitoring stopped. The counters it incremented were still in the
API's `/metrics`, so the next scrape picks them up; only the samples in between are missing.

---

## Clip 3: tracing, logging, and the baseline

### 7. Structured logs for one request

```bash
cd ../m3
podman-compose run --rm cli reset CASE-10431
podman-compose run --rm cli submit A01 | head -1          # note the request id
podman logs taco-app_complaint-api_1 2>&1 | grep '<request id>' | jq -c 'del(.ts, .level, .logger, .request_id)'
```

```
{"event":"model_call","node":"investigate","ms":345.7,"input_tokens":761,"output_tokens":27}
{"event":"tool_call","tool":"get_order","status":"ok","error_kind":null,"retries":0,"ms":1.2}
...
{"event":"model_call","node":"propose","ms":4327.5,"input_tokens":1953,"output_tokens":375}
{"event":"workflow_run","case_id":"CASE-10431","thread_id":"CASE-10431:...","run":"submit","status":"pending_review","reason":null,"seconds":9.0}
{"event":"http_request","method":"POST","route":"/complaints","status":201,"ms":9020.1,"actor":"rep-kop-01"}
```

**GPU host**: the same id in the gateway log, once per model call:

```bash
podman logs gateway 2>&1 | grep '<request id>'
```

```
{"ts":"...","request_id":"<request id>","client":"...","method":"POST","route":"/v1/chat/completions","status":200,"request_time":0.346,"upstream_response_time":"0.345","bytes":1234}
```

Walk through `m3/app/observability.py` (metric definitions, the JSON formatter, `observe()`), the
request-id middleware in `m3/app/main.py`, and `request_id_hook` in
`shared/taco_shared/agent/model.py`. Logs carry ids, timings, counts, and outcomes; they never carry
complaint text or proposals. Capturing content for review is a separate, restricted store (module 5),
and the audit record is another (module 6).

### 8. Record the baseline

Off camera, run the steady workload and generate the report:

```bash
cd ../m4
podman-compose run --rm load --mode app --shape steady --label app-baseline
podman-compose run --rm baseline --label app-baseline
```

On camera, open `results/baseline-app-baseline-<ts>.md`: client, application, and server numbers for
the same window, the run settings (users, seed, dataset `load-v1`), the execution host and network
path, and the deployment versions. Compare it with the thresholds in `prometheus/rules/taco.yml`
(`WorkflowSlow` at 20 s p95, `InferenceQueueing` when anything waits for 2 minutes) and adjust them if
the baseline says otherwise. Module 5 compares every change against this file.

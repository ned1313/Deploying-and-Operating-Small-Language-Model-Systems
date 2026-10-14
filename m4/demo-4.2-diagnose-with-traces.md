# Demo 4.2: Diagnose a failure with traces

Goal: trace the complaint workflow in LangSmith, then inject a slow and a failing case API and locate
the bottleneck in the trace, the structured logs, and the dashboards. Conclude that it is an
infrastructure failure, not a model-quality issue.

Prerequisites: Demo 4.1 setup; LangSmith settings in `m4/.env` and `TRACING_ENV_FILE=../m4/.env` in
`m3/.env`. Commands run from `m3/` unless noted. All data is synthetic.

Before each take:

```bash
podman-compose run --rm cli reset CASE-10431 CASE-10431-F01
podman-compose run --rm cli case-fault clear
```

---

## Clip 3 (tracing concepts) and Clip 4: collecting data with LangSmith

### 1. Configure tracing

Show `m4/.env` with the key hidden (`grep LANGSMITH ../m4/.env | grep -v API_KEY`):

```
LANGSMITH_TRACING=true
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_PROJECT=taco-alley
LANGSMITH_TRACING_SAMPLING_RATE=1.0
LANGSMITH_HIDE_INPUTS=false
LANGSMITH_HIDE_OUTPUTS=false
```

```bash
cd ../m3
podman-compose up -d --force-recreate complaint-api
podman logs taco-app_complaint-api_1 2>&1 | grep complaint_api_ready | jq -c '{tracing, trace_project, app_version, model, prompt_version}'
```

Talking points:

- No tracing code in the workflow: LangGraph and LangChain emit spans when `LANGSMITH_TRACING=true`.
  The service adds the run name (`submit`, `decision`, `clarify`, `retry`), tags (`scenario:<id>`,
  `injected_fault:<mode>`), and metadata (`request_id`, `case_id`, `thread_id`, `store_id`, `actor_id`,
  and the deployment versions) in `_run_config()` in `m3/app/service.py`.
- Export runs on a background thread. If LangSmith is unreachable, requests still succeed; the SDK
  logs a warning (covered by `test_unreachable_trace_endpoint_does_not_fail_requests`).
- `LANGSMITH_TRACING_SAMPLING_RATE` controls how many runs are exported; production usually samples.

### 2. Trace one complaint through approval

```bash
podman-compose run --rm cli submit A01
podman-compose run --rm cli approve CASE-10431
```

In LangSmith, project **taco-alley**:

- The `submit` trace: `intake`, then `investigate` (a `ChatOpenAI` span) and `tools` (a span per tool)
  alternating four times, `check_evidence`, `propose` (the structured-output call), `validate`,
  `human_review`. Open one `ChatOpenAI` span to show the prompt, tool definitions, and token counts.
- The metadata panel: `request_id` matches the CLI output and the log lines from Demo 4.1.
- The `decision` trace is separate: the run ended at the approval interrupt. Filter by
  `metadata.thread_id` (or open **Threads**) to see both runs of the case together. The time between
  them is approval wait, reported as `taco_approval_wait_seconds`, not as a span.

### 3. A slow dependency

```bash
podman-compose run --rm cli reset CASE-10431
podman-compose run --rm cli case-fault latency --ms 2500 --count 1
podman-compose run --rm cli submit A01 | head -2
```

`--after 1` (the default) skips the API's own case lookup at submit, so the delay lands on the
`get_case_status` tool call inside the workflow. The case still reaches `pending_review`, about 2.5 s
slower.

Find the bottleneck three ways:

1. **Trace**: in the `submit` trace, the `get_case_status` tool span (and its `tools` parent) is the
   long bar; the `ChatOpenAI` spans have their usual durations.
2. **Logs**: `podman logs taco-app_complaint-api_1 2>&1 | grep '<request id>' | jq -c 'select(.event=="tool_call")'`
   shows `get_case_status` at about 2500 ms; the `model_call` lines are unchanged.
3. **Dashboard** (**Complaint workflow**): *Tool latency p95* for `get_case_status` jumps while
   *Model call latency* stays flat.

### 4. A failing dependency

```bash
podman-compose run --rm cli case-fault timeout
podman-compose run --rm cli submit F01
podman-compose run --rm cli case-fault clear
```

```
POST /complaints as rep-kop-01 -> HTTP 201  (request 68a0f9c2e2c844c78d2156b4d2d18e73)
Case CASE-10431-F01  status: escalated (dependency_unavailable)  store KOP  submitted by rep-kop-01
  counters: tool_calls=3  repeated_calls=0  nudges=0  corrections=0  case_api_retries=2  model_calls=4
```

Every case API request now hangs past the client's 3 s timeout. The tool tries three times (two
retries with backoff), returns `unavailable`, and the graph escalates instead of guessing.

- **Trace**: `get_case_status` takes about 11 s, then `check_evidence` routes to `escalate`. There is
  no `propose` span: the model was never asked to resolve the case without the evidence.
- **Log**: `{"event": "tool_call", "tool": "get_case_status", "status": "error", "error_kind": "unavailable", "retries": 2, "ms": 10512.7}`
- **Dashboard**: *Tool calls by result* shows `unavailable`, *Case API retries* rises, and the
  `ToolErrors` alert goes pending, then fires after a minute (Prometheus **Alerts**, or *Alerts firing*
  on the dashboard).

Conclusion for the clip: model latency and token counts were normal in both runs; the time and the
failure sit in one dependency. The fix is in the case API (or its timeout and retry budget), not in
the prompt or the model.

### 5. Redaction before export

To show what leaves the deployment, turn on the redaction switches in `m4/.env`:

```
LANGSMITH_HIDE_INPUTS=true
LANGSMITH_HIDE_OUTPUTS=true
```

```bash
podman-compose up -d --force-recreate complaint-api
podman-compose run --rm cli reset CASE-10431
podman-compose run --rm cli submit A01
```

The new trace has the same spans, timings, and metadata, but no prompts, complaint text, tool
results, or proposals. Set both back to `false` afterwards and recreate `complaint-api`.

### 6. Hosted traces vs a local trace store

Discussion only; see "Local tracing alternative" in [README.md](README.md). Synthetic data makes the
hosted service acceptable here. A deployment that may not send complaint text to a third party either
keeps redaction on or replaces LangSmith with OpenTelemetry, a collector, and Tempo on the desktop.

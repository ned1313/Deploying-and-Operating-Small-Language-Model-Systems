"""Generates the provisioned Grafana dashboards. Edit the panel lists here, then run:

    python observability/grafana/build_dashboards.py

and commit the regenerated JSON files in observability/grafana/dashboards/.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}
RI = "$__rate_interval"


def q(expr: str, legend: str = "", instant: bool = False, table: bool = False) -> dict:
    target = {"datasource": DS, "expr": expr, "legendFormat": legend or "__auto", "refId": ""}
    if instant:
        target["instant"], target["range"] = True, False
    if table:
        target["format"] = "table"
    return target


def p95(metric: str, by: str = "", window: str = RI, quantile: float = 0.95) -> str:
    group = f"le, {by}" if by else "le"
    return f"histogram_quantile({quantile}, sum by ({group}) (rate({metric}_bucket[{window}])))"


def ts(title: str, targets: list[dict], unit: str = "short", w: int = 12, h: int = 8, desc: str = "",
       stack: bool = False, minimum: float | None = 0) -> dict:
    defaults: dict = {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 10, "showPoints": "never",
                                               "stacking": {"mode": "normal" if stack else "none"}}}
    if minimum is not None:
        defaults["min"] = minimum
    return {"type": "timeseries", "title": title, "description": desc, "datasource": DS, "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
            "gridPos": {"w": w, "h": h}}


def stat(title: str, expr: str, unit: str = "short", thresholds: list[tuple[float | None, str]] | None = None,
         w: int = 4, desc: str = "", decimals: int | None = None) -> dict:
    steps = [{"value": value, "color": color} for value, color in (thresholds or [(None, "green")])]
    defaults: dict = {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {"type": "stat", "title": title, "description": desc, "datasource": DS, "targets": [q(expr, instant=True)],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"colorMode": "background", "graphMode": "none", "reduceOptions": {"calcs": ["lastNotNull"]}},
            "gridPos": {"w": w, "h": 4}}


def row(title: str) -> dict:
    return {"type": "row", "title": title, "collapsed": False, "panels": [], "gridPos": {"w": 24, "h": 1}}


def layout(panels: list[dict]) -> list[dict]:
    x = y = row_height = 0
    for number, panel in enumerate(panels, start=1):
        grid = panel["gridPos"]
        if panel["type"] == "row" or x + grid["w"] > 24:
            x, y = 0, y + row_height
            row_height = 0
        grid.update(x=x, y=y)
        panel["id"] = number
        for index, target in enumerate(panel.get("targets", [])):
            target["refId"] = chr(ord("A") + index)
        x += grid["w"]
        row_height = max(row_height, grid["h"])
        if panel["type"] == "row":
            x, y, row_height = 0, y + 1, 0
    return panels


def dashboard(uid: str, title: str, description: str, panels: list[dict]) -> dict:
    return {"uid": uid, "title": title, "description": description, "tags": ["taco-alley", "module-4"],
            "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True, "graphTooltip": 1,
            "refresh": "5s", "time": {"from": "now-30m", "to": "now"},
            "links": [{"type": "dashboards", "tags": ["taco-alley"], "asDropdown": False, "title": "Taco Alley"}],
            "templating": {"list": []}, "annotations": {"list": []}, "panels": layout(panels)}


SATURATION = [(None, "green"), (0.7, "orange"), (0.9, "red")]

VLLM = dashboard("taco-vllm", "Model serving (vLLM)",
                 "Server-side view of the model: queueing, KV cache, latency phases, and token throughput.", [
    stat("Requests running", "sum(vllm:num_requests_running)"),
    stat("Requests waiting", "sum(vllm:num_requests_waiting)", thresholds=[(None, "green"), (1, "orange"), (4, "red")]),
    stat("KV-cache usage", "max(vllm:kv_cache_usage_perc)", "percentunit", SATURATION),
    stat("Generation tokens/s", f"sum(rate(vllm:generation_tokens_total[1m]))", decimals=0),
    stat("e2e latency p95 (5m)", "taco:vllm_e2e_seconds:p95_5m", "s"),
    stat("Preemptions (15m)", "sum(increase(vllm:num_preemptions_total[15m]))", thresholds=[(None, "green"), (1, "red")],
         decimals=0),
    row("Load and saturation"),
    ts("Requests running and waiting", [q("sum(vllm:num_requests_running)", "running"),
                                        q("sum(vllm:num_requests_waiting)", "waiting")],
       desc="Waiting > 0 means the scheduler has no room: batch size or KV cache is the limit."),
    ts("KV-cache usage", [q("max(vllm:kv_cache_usage_perc)", "kv cache")], "percentunit",
       desc="Share of KV-cache blocks in use. Near 100% the scheduler preempts or queues requests."),
    ts("Tokens per second", [q(f"sum(rate(vllm:prompt_tokens_total[{RI}]))", "prompt (prefill)"),
                             q(f"sum(rate(vllm:generation_tokens_total[{RI}]))", "generation (decode)")]),
    ts("Finished requests by reason", [q(f"sum by (finished_reason) (rate(vllm:request_success_total[{RI}]))",
                                         "{{finished_reason}}"),
                                       q(f"sum(increase(vllm:num_preemptions_total[{RI}]))", "preemptions")], "reqps",
       desc="finished_reason=length means a response hit max_tokens."),
    row("Latency (server side)"),
    ts("End-to-end latency", [q(p95("vllm:e2e_request_latency_seconds", quantile=0.5), "p50"),
                              q(p95("vllm:e2e_request_latency_seconds"), "p95")], "s",
       desc="From arrival at vLLM to the last token. Excludes the network and the gateway."),
    ts("Queue time and time to first token (p95)", [q(p95("vllm:request_queue_time_seconds"), "queue"),
                                                    q(p95("vllm:time_to_first_token_seconds"), "time to first token")], "s"),
    ts("Prefill and decode time (p95)", [q(p95("vllm:request_prefill_time_seconds"), "prefill"),
                                         q(p95("vllm:request_decode_time_seconds"), "decode")], "s",
       desc="Prefill is compute-bound on prompt length; decode is memory-bandwidth-bound on output length."),
    ts("Time per output token", [q(p95("vllm:request_time_per_output_token_seconds", quantile=0.5), "p50"),
                                 q(p95("vllm:request_time_per_output_token_seconds"), "p95")], "s"),
])

HOST = dashboard("taco-host", "Model host", "GPU and host resources on the dedicated model server.", [
    stat("GPU utilization", "max(taco:gpu_utilization:ratio)", "percentunit", SATURATION),
    stat("VRAM used", "max(taco:gpu_memory_used:bytes / taco:gpu_memory_total:bytes)", "percentunit",
         desc="vLLM preallocates VRAM for weights and the KV cache at startup, so this stays high even when idle."),
    stat("GPU power", "max(taco:gpu_power:watts)", "watt"),
    stat("GPU temperature", "max(taco:gpu_temperature:celsius)", "celsius", [(None, "green"), (80, "orange"), (88, "red")]),
    stat("Host CPU", "max(taco:host_cpu_utilization:ratio)", "percentunit", SATURATION),
    stat("Targets up", 'sum(up{instance="model-host"})', thresholds=[(None, "red"), (3, "green")],
         desc="vllm, node, and gpu jobs on the model host."),
    row("GPU"),
    ts("GPU utilization", [q("taco:gpu_utilization:ratio", "gpu")], "percentunit",
       desc="Share of time a kernel ran. High utilization alone does not mean useful throughput."),
    ts("VRAM", [q("taco:gpu_memory_used:bytes", "used"), q("taco:gpu_memory_total:bytes", "total")], "bytes"),
    ts("GPU power", [q("taco:gpu_power:watts", "power")], "watt"),
    ts("GPU temperature", [q("taco:gpu_temperature:celsius", "temperature")], "celsius"),
    row("Host"),
    ts("Host CPU utilization", [q("taco:host_cpu_utilization:ratio", "cpu")], "percentunit"),
    ts("Host memory used", [q("taco:host_memory_used:ratio", "memory")], "percentunit"),
    ts("Network", [q(f'sum(rate(node_network_receive_bytes_total{{job="node", device!~"lo|veth.*|podman.*|cni.*|docker.*"}}[{RI}]))', "receive"),
                   q(f'sum(rate(node_network_transmit_bytes_total{{job="node", device!~"lo|veth.*|podman.*|cni.*|docker.*"}}[{RI}]))', "transmit")],
       "Bps", w=24),
])

WORKFLOW = dashboard("taco-workflow", "Complaint workflow",
                     "Application view: requests, workflow outcomes, where time goes, and dependency errors.", [
    stat("Runs in progress", "sum(taco_workflow_runs_in_progress)"),
    stat("Run p95 (5m)", "taco:workflow_run_seconds:p95_5m", "s", [(None, "green"), (15, "orange"), (20, "red")]),
    stat("Queue wait p95 (5m)", p95("taco_workflow_queue_wait_seconds", window="5m"), "s",
         [(None, "green"), (5, "orange"), (30, "red")]),
    stat("Tool infra errors (5m)", "taco:tool_infra_errors:ratio_5m", "percentunit",
         [(None, "green"), (0.01, "orange"), (0.05, "red")]),
    stat("Approval wait p50 (1h)", p95("taco_approval_wait_seconds", window="1h", quantile=0.5), "s",
         desc="Time a proposal waited for a reviewer. Reported separately from processing latency."),
    stat("Alerts firing", 'count(ALERTS{alertstate="firing"}) or vector(0)', thresholds=[(None, "green"), (1, "red")]),
    row("Traffic and outcomes"),
    ts("Requests by route", [q(f'sum by (route, status) (rate(taco_http_requests_total{{route!~"/metrics|/healthz"}}[{RI}]))',
                               "{{route}} {{status}}")], "reqps"),
    ts("Workflow runs by resulting status", [q(f"sum by (status) (rate(taco_workflow_runs_total[{RI}])) * 60",
                                               "{{status}}")], "short", stack=True,
       desc="Runs per minute: pending_review, needs_clarification, escalated, recorded, failed_retryable."),
    row("Where the time goes"),
    ts("Workflow run latency (excludes approval wait)",
       [q(p95("taco_workflow_run_seconds", quantile=0.5), "p50"), q(p95("taco_workflow_run_seconds"), "p95")], "s"),
    ts("Model call latency: application vs server",
       [q(p95("taco_model_seconds"), "app-observed model call p95"),
        q("taco:vllm_e2e_seconds:p95_5m", "vLLM e2e p95 (5m)"),
        q(p95("vllm:request_queue_time_seconds"), "vLLM queue p95")], "s",
       desc="The gap between the app's view and vLLM's e2e time is network, gateway, and client overhead."),
    ts("Node latency p95", [q(p95("taco_workflow_node_seconds", "node"), "{{node}}")], "s"),
    ts("Tool latency p95", [q(p95("taco_tool_seconds", "tool"), "{{tool}}")], "s",
       desc="get_case_status calls the case API; the other tools read local SQLite or policy files."),
    ts("Tool calls by result", [q(f"sum by (tool, status, error_kind) (rate(taco_tool_calls_total[{RI}])) * 60",
                                  "{{tool}} {{status}} {{error_kind}}")], "short",
       desc="Per minute. error_kind=unavailable is infrastructure; not_found is a data outcome."),
    ts("Case API retries", [q(f"sum(rate(taco_case_api_retries_total[{RI}])) * 60", "retries per minute")]),
    row("Capacity and tokens"),
    ts("Run slots and queue", [q("sum(taco_workflow_runs_in_progress)", "runs in progress"),
                               q(p95("taco_workflow_queue_wait_seconds"), "queue wait p95 (s)"),
                               q(f"sum(increase(taco_workflow_queue_rejected_total[{RI}]))", "rejected (503)")]),
    ts("Tokens per workflow run", [q(f"sum by (direction) (rate(taco_model_tokens_total[5m])) / "
                                     f"scalar(sum(rate(taco_workflow_runs_total[5m])))", "{{direction}}")],
       desc="Averaged over 5 minutes; includes every model call in a run (investigation and proposal)."),
    ts("Approval wait", [q(p95("taco_approval_wait_seconds", window="15m", quantile=0.5), "p50"),
                         q(p95("taco_approval_wait_seconds", window="15m"), "p95")], "s"),
    ts("Complaint API process", [q('rate(process_cpu_seconds_total{job="complaint-api"}[1m])', "cpu cores"),
                                 q('process_resident_memory_bytes{job="complaint-api"} / 1e9', "rss (GB)")]),
    row("Deployment"),
    {"type": "table", "title": "Deployment versions", "datasource": DS,
     "targets": [q("taco_build_info", instant=True, table=True)],
     "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True, "__name__": True,
                                                                           "job": True, "host": True}}}],
     "fieldConfig": {"defaults": {}, "overrides": []}, "options": {"showHeader": True},
     "gridPos": {"w": 24, "h": 4}},
])


QUALITY = dashboard("taco-quality", "Quality and cost",
                    "Module 5: sampled output quality by model, the review queue, and an estimated cost per successful "
                    "resolution. Quality ratios cover the last 20 scored samples; run the quality tool to score new ones.", [
    stat("Checks pass (last 20)", 'min(taco_quality_recent_ratio{signal="checks_passed"})', "percentunit",
         [(None, "red"), (0.9, "green")], desc="Deterministic checks on the sampled proposals (QualityDegraded below 90%)."),
    stat("Label pass (last 20)", 'min(taco_quality_recent_ratio{signal="label_passed"})', "percentunit",
         [(None, "red"), (0.78, "green")], desc="grade() against the synthetic labels; production traffic has none."),
    stat("Successful (last 20)", 'min(taco_quality_recent_ratio{signal="successful"})', "percentunit",
         [(None, "red"), (0.8, "green")]),
    stat("Review queue", "sum(taco_quality_review_queue)", thresholds=[(None, "green"), (1, "orange"), (10, "red")],
         desc="Flagged samples without a human review. Work them at /ui/quality?actor=qa-01."),
    stat("Unscored samples", "sum(taco_quality_unscored)", thresholds=[(None, "green"), (1, "orange")],
         desc="Run `podman-compose run --rm quality score` from tools/."),
    stat("Cost per successful resolution",
         f'$hourly_cost / clamp_min(sum(rate(taco_workflow_runs_total{{status=~"recorded|escalated"}}[15m])) * 3600 '
         f'* scalar(min(taco_quality_recent_ratio{{signal="successful"}})), 1e-9)', "currencyUSD",
         desc="Hourly cost (dashboard variable) / (recorded per hour x successful share). The cost report has the "
              "exact numbers and assumptions.", decimals=3),
    row("Quality by model"),
    ts("Quality signals (last 20 scored samples)", [q("taco_quality_recent_ratio", "{{model}} {{signal}}")],
       "percentunit", w=24, desc="Each point is the window at that moment; a model swap shows up as a new series."),
    ts("Sampled outcomes", [q("taco_quality_samples", "{{status}}")], stack=True),
    ts("Review queue and unscored samples", [q("sum(taco_quality_review_queue)", "review queue"),
                                            q("sum(taco_quality_unscored)", "unscored")]),
    row("Throughput and cost"),
    ts("Recorded resolutions per hour", [q('sum(rate(taco_workflow_runs_total{status=~"recorded|escalated"}[15m])) * 3600',
                                         "recorded per hour (15m rate)")],
       desc="Approved actions recorded through the case API, from the application's own counters."),
    ts("GPU power and utilization", [q("taco:gpu_power:watts", "power (W)"),
                                     q("taco:gpu_utilization:ratio * 100", "utilization (%)")],
       desc="The GPU host costs the same per hour whether it is busy or idle; cost per resolution falls only when "
            "more successful resolutions fit in the hour."),
    row("Deployment"),
    {"type": "table", "title": "Deployment versions (current model behind the alias)", "datasource": DS,
     "targets": [q("taco_build_info", instant=True, table=True)],
     "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True, "__name__": True,
                                                                           "job": True, "host": True}}}],
     "fieldConfig": {"defaults": {}, "overrides": []}, "options": {"showHeader": True},
     "gridPos": {"w": 24, "h": 4}},
])
QUALITY["tags"] = ["taco-alley", "module-5"]
QUALITY["refresh"] = "30s"
QUALITY["time"] = {"from": "now-3h", "to": "now"}
QUALITY["templating"]["list"] = [{
    "type": "textbox", "name": "hourly_cost", "label": "Hourly cost (USD)", "query": "0.24",
    "current": {"text": "0.24", "value": "0.24"}, "options": [{"selected": True, "text": "0.24", "value": "0.24"}],
    "description": "On-prem estimate from tools/.env defaults: amortized host + GPU power + desktop share."}]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, board in (("model-serving", VLLM), ("model-host", HOST), ("complaint-workflow", WORKFLOW),
                        ("quality-and-cost", QUALITY)):
        (OUT / f"{name}.json").write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {name}.json ({len(board['panels'])} panels)")


if __name__ == "__main__":
    main()

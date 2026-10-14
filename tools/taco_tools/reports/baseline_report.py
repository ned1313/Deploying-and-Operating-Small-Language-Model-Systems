"""Documented performance baseline for one Locust run (module 4).

    podman-compose run --rm baseline                        # newest load-*-meta.json in results/load/
    podman-compose run --rm baseline results/load/load-app-steady-<ts>-meta.json --label app-baseline

Queries Prometheus over the run window and combines it with Locust's client-side numbers. Writes
results/baseline/baseline-<label>-<ts>.json and .md. Module 5 compares later runs against these files.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from taco_shared.reporting import safe_slug, timestamp_slug

RESULTS = Path(os.environ.get("RESULTS_DIR", "/app/results/baseline"))
LOAD_RESULTS = Path(os.environ.get("LOAD_RESULTS_DIR", "/app/results/load"))


def quantile(metric: str, q: float, window: str, by: str = "") -> str:
    group = f"le, {by}" if by else "le"
    return f"histogram_quantile({q}, sum by ({group}) (increase({metric}_bucket[{window}])))"


def count(expr: str) -> str:
    """increase() extrapolates to the window edges; round to whole events and report 0 when no series exist."""
    return f"round(sum({expr})) or vector(0)"


def queries(w: str) -> dict[str, tuple[str, str]]:
    """name -> (PromQL evaluated at the end of the run, unit)."""
    return {
        "app.workflow_run_p50_s": (quantile("taco_workflow_run_seconds", 0.5, w), "s"),
        "app.workflow_run_p95_s": (quantile("taco_workflow_run_seconds", 0.95, w), "s"),
        "app.model_call_p95_s": (quantile("taco_model_seconds", 0.95, w), "s"),
        "app.queue_wait_p95_s": (quantile("taco_workflow_queue_wait_seconds", 0.95, w), "s"),
        "app.max_runs_in_progress": (f"max_over_time(sum(taco_workflow_runs_in_progress)[{w}:5s])", ""),
        "app.queue_rejected": (count(f"increase(taco_workflow_queue_rejected_total[{w}])"), ""),
        "app.tool_infra_errors": (count(f'increase(taco_tool_calls_total{{error_kind="unavailable"}}[{w}])'), ""),
        "app.case_api_retries": (count(f"increase(taco_case_api_retries_total[{w}])"), ""),
        "app.approval_wait_p50_s": (quantile("taco_approval_wait_seconds", 0.5, w), "s"),
        "app.process_cpu_cores_mean": (f'avg_over_time(rate(process_cpu_seconds_total{{job="complaint-api"}}[1m])[{w}:5s])', ""),
        "vllm.e2e_p50_s": (quantile("vllm:e2e_request_latency_seconds", 0.5, w), "s"),
        "vllm.e2e_p95_s": (quantile("vllm:e2e_request_latency_seconds", 0.95, w), "s"),
        "vllm.ttft_p95_s": (quantile("vllm:time_to_first_token_seconds", 0.95, w), "s"),
        "vllm.queue_p95_s": (quantile("vllm:request_queue_time_seconds", 0.95, w), "s"),
        "vllm.requests": (count(f"increase(vllm:e2e_request_latency_seconds_count[{w}])"), ""),
        "vllm.prompt_tokens_per_s": (f"sum(increase(vllm:prompt_tokens_total[{w}])) / {w[:-1]}", "tok/s"),
        "vllm.generation_tokens_per_s": (f"sum(increase(vllm:generation_tokens_total[{w}])) / {w[:-1]}", "tok/s"),
        "vllm.max_running": (f"max_over_time(sum(vllm:num_requests_running)[{w}:5s])", ""),
        "vllm.max_waiting": (f"max_over_time(sum(vllm:num_requests_waiting)[{w}:5s])", ""),
        "vllm.max_kv_cache": (f"max_over_time(max(vllm:kv_cache_usage_perc)[{w}:5s])", "ratio"),
        "vllm.preemptions": (count(f"increase(vllm:num_preemptions_total[{w}])"), ""),
        "gpu.utilization_mean": (f"avg_over_time(taco:gpu_utilization:ratio[{w}])", "ratio"),
        "gpu.utilization_max": (f"max_over_time(taco:gpu_utilization:ratio[{w}])", "ratio"),
        "gpu.vram_used_max_gib": (f"max_over_time(taco:gpu_memory_used:bytes[{w}]) / 1073741824", "GiB"),
        "gpu.power_mean_w": (f"avg_over_time(taco:gpu_power:watts[{w}])", "W"),
        "host.cpu_mean": (f"avg_over_time(taco:host_cpu_utilization:ratio[{w}])", "ratio"),
    }


GROUPED = {
    "app.workflow_outcomes": "round(sum by (status) (increase(taco_workflow_runs_total[{w}])))",
    "app.node_p95_s": "histogram_quantile(0.95, sum by (le, node) (increase(taco_workflow_node_seconds_bucket[{w}])))",
    "app.tool_p95_s": "histogram_quantile(0.95, sum by (le, tool) (increase(taco_tool_seconds_bucket[{w}])))",
}


def prom(url: str, expr: str, at: float) -> list[dict[str, Any]]:
    response = httpx.get(f"{url}/api/v1/query", params={"query": expr, "time": at}, timeout=30)
    response.raise_for_status()
    return response.json()["data"]["result"]


def number(value: str) -> float | None:
    result = float(value)
    return None if math.isnan(result) or math.isinf(result) else round(result, 4)


def locust_stats(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    rows = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows[row["Name"]] = {"requests": int(row["Request Count"]), "failures": int(row["Failure Count"]),
                                 "median_s": float(row["Median Response Time"]) / 1000,
                                 "p95_s": float(row["95%"]) / 1000 if row["95%"] not in ("N/A", "") else None,
                                 "mean_s": round(float(row["Average Response Time"]) / 1000, 3),
                                 "rps": round(float(row["Requests/s"]), 4)}
    return rows


def fmt(value: Any, unit: str) -> str:
    if value is None:
        return "n/a"
    if unit == "ratio":
        return f"{value * 100:.1f}%"
    return f"{value:.3g} {unit}".strip()


def markdown(report: dict[str, Any]) -> str:
    meta = report["run"]
    lines = [f"# Performance baseline: {report['label']}", "",
             f"- Run: `{meta['label']}` ({meta['mode']}, {meta['shape']}), {meta['users']} users"
             + (f", {meta['burst_users']} during the burst" if meta.get("burst_users") else "")
             + f", {meta['duration_seconds']} s, seed {meta['seed']}",
             f"- Window: {meta['started_at']} to {meta['ended_at']}",
             f"- Model: `{meta['model']}`, dataset `{meta['dataset_version']}`",
             f"- Execution host: {meta['execution_host']}; network path: {meta['network_path']}",
             f"- Deployment versions: {report.get('deployment') or 'n/a'}", "",
             "## Client side (Locust)", "", "| Request | Count | Failures | Median | p95 | Mean | Req/s |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for name, row in report["locust"].items():
        lines.append(f"| {name} | {row['requests']} | {row['failures']} | {row['median_s']:.2f} s | "
                     f"{fmt(row['p95_s'], 's')} | {row['mean_s']:.2f} s | {row['rps']} |")
    lines += ["", f"Outcomes reported by the load generator: {meta.get('outcomes') or {}}", "",
              "## Server side (Prometheus, same window)", "", "| Signal | Value |", "| --- | --- |"]
    for name, (value, unit) in report["metrics"].items():
        lines.append(f"| {name} | {fmt(value, unit)} |")
    for name, values in report["grouped"].items():
        lines.append(f"| {name} | " + ", ".join(f"{key}: {fmt(v, '')}" for key, v in sorted(values.items())) + " |")
    lines += ["", "## Reading the numbers", "",
              "- Locust latency includes the network and the gateway; `vllm.*` is measured inside the model server.",
              "- `app.workflow_run_*` excludes approval wait, which is reported separately.",
              "- Revisit the alert thresholds in `observability/prometheus/rules/taco.yml` against these values.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("meta", nargs="?", help="load metadata file (default: newest results/load/load-*-meta.json)")
    parser.add_argument("--label", help="baseline name (default: the run label)")
    parser.add_argument("--prometheus-url", default=os.environ.get("PROMETHEUS_URL", "http://prometheus:9090"))
    args = parser.parse_args()
    candidates = [Path(args.meta)] if args.meta else sorted(LOAD_RESULTS.glob("load-*-meta.json"), key=lambda p: p.stat().st_mtime)
    if not candidates or not candidates[-1].exists():
        sys.exit("no load metadata found; run the load service first")
    meta_path = candidates[-1]
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    window = f"{max(int(meta['ended_epoch'] - meta['started_epoch']), 60)}s"
    at = meta["ended_epoch"]

    metrics: dict[str, tuple[float | None, str]] = {}
    for name, (expr, unit) in queries(window).items():
        result = prom(args.prometheus_url, expr, at)
        metrics[name] = (number(result[0]["value"][1]) if result else None, unit)
    grouped = {}
    for name, template in GROUPED.items():
        result = prom(args.prometheus_url, template.format(w=window), at)
        key = "status" if "outcomes" in name else ("node" if "node" in name else "tool")
        grouped[name] = {r["metric"].get(key, "?"): number(r["value"][1]) for r in result}
    build = prom(args.prometheus_url, "taco_build_info", at)
    deployment = {k: v for k, v in build[0]["metric"].items() if k not in {"__name__", "job", "instance", "host"}} if build else None

    label = safe_slug(args.label or meta["label"])
    report = {"label": label, "run": meta, "window": window, "deployment": deployment,
              "locust": locust_stats(meta_path.with_name(meta["files"]["stats_csv"])),
              "metrics": metrics, "grouped": grouped}
    out = RESULTS / f"baseline-{label}-{timestamp_slug()}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(markdown(report))
    print(f"wrote {out.name}.json and .md")
    return 0


if __name__ == "__main__":
    sys.exit(main())

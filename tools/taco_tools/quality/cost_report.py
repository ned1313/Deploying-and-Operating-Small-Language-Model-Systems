#!/usr/bin/env python3
"""Demo 5.3: cost per successful resolution for one or more app load runs.

    cost app-r1-steady                     one run (the label given to `load --label`)
    cost app-r1-steady app-r1-concurrency  side by side

For each run: the Locust metadata (window, users), Prometheus over the same window (GPU power and
utilization, tokens), and the quality scores of the samples created in the window (samples are scored
first). Cost assumptions come from the environment (tools/.env) and are printed with every result.
Writes results/cost/cost-<labels>-<ts>.{json,md}.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from taco_shared.load import load_cases
from taco_shared.quality.store import QualityStore
from taco_shared.reporting import safe_slug, timestamp_slug
from taco_tools.quality.quality import QUALITY_DB, score_pending
from taco_tools.reports.baseline_report import number, prom

RESULTS_ROOT = Path(os.environ.get("RESULTS_ROOT") or "/app/results")
HOURS_PER_YEAR = 8760


def assumptions() -> dict[str, float]:
    def env(name: str, default: float) -> float:
        return float(os.environ.get(name) or default)
    return {"gpu_host_price_usd": env("COST_GPU_HOST_PRICE_USD", 3000), "amortization_years": env("COST_AMORTIZATION_YEARS", 3),
            "electricity_usd_per_kwh": env("COST_ELECTRICITY_USD_PER_KWH", 0.15),
            "host_overhead_watts": env("COST_HOST_OVERHEAD_WATTS", 120),
            "desktop_share_usd_per_hour": env("COST_DESKTOP_SHARE_USD_PER_HOUR", 0.05),
            "cloud_gpu_usd_per_hour": env("COST_CLOUD_GPU_USD_PER_HOUR", 1.00),
            "review_usd_per_case": env("COST_REVIEW_USD_PER_CASE", 0)}


def find_meta(label: str) -> dict[str, Any]:
    matches = sorted((RESULTS_ROOT / "load").glob(f"load-{safe_slug(label)}-*-meta.json"), key=lambda p: p.stat().st_mtime)
    if not matches:
        sys.exit(f"no load run labeled {label} in results/load/")
    meta = json.loads(matches[-1].read_text(encoding="utf-8"))
    meta["_file"] = matches[-1].name
    return meta


def window_metrics(url: str, meta: dict[str, Any]) -> dict[str, float | None]:
    seconds = max(int(meta["ended_epoch"] - meta["started_epoch"]), 60)
    w, at = f"{seconds}s", meta["ended_epoch"]
    queries = {"gpu_power_w": f"avg_over_time(taco:gpu_power:watts[{w}])",
               "gpu_utilization": f"avg_over_time(taco:gpu_utilization:ratio[{w}])",
               "host_cpu": f"avg_over_time(taco:host_cpu_utilization:ratio[{w}])",
               "input_tokens": f'sum(increase(taco_model_tokens_total{{direction="input"}}[{w}]))',
               "output_tokens": f'sum(increase(taco_model_tokens_total{{direction="output"}}[{w}]))',
               "vllm_max_running": f"max_over_time(sum(vllm:num_requests_running)[{w}:5s])",
               "workflow_p95_s": f"histogram_quantile(0.95, sum by (le) (increase(taco_workflow_run_seconds_bucket[{w}])))"}
    result: dict[str, float | None] = {}
    for name, expr in queries.items():
        try:
            rows = prom(url, expr, at)
            result[name] = number(rows[0]["value"][1]) if rows else None
        except Exception:  # noqa: BLE001 - a missing metric is reported as n/a
            result[name] = None
    return result


def quality_in_window(store: QualityStore, meta: dict[str, Any]) -> dict[str, Any]:
    start = datetime.fromtimestamp(meta["started_epoch"], timezone.utc).isoformat(timespec="seconds")
    end = datetime.fromtimestamp(meta["ended_epoch"] + 5, timezone.utc).isoformat(timespec="seconds")
    load_ids = {case["case_id"] for case in load_cases()}
    rows = store.scored(since=start, until=end, case_ids=load_ids)
    final = [row for row in rows if row["successful"] is not None]
    return {"samples": len(rows), "final": len(final), "successful": sum(1 for row in final if row["successful"]),
            "recorded": sum(1 for row in rows if row["status"] in {"recorded", "escalated"}),
            "pending": len(rows) - len(final),
            "failed_checks": sum(1 for row in rows if row["checks_passed"] == 0),
            "failed_labels": sum(1 for row in rows if row["label_passed"] == 0)}


def cost(meta: dict[str, Any], metrics: dict[str, Any], quality: dict[str, Any], a: dict[str, float]) -> dict[str, Any]:
    hours = (meta["ended_epoch"] - meta["started_epoch"]) / 3600
    power = (metrics.get("gpu_power_w") or 0) + a["host_overhead_watts"]
    on_prem_hour = (a["gpu_host_price_usd"] / (a["amortization_years"] * HOURS_PER_YEAR)
                    + power / 1000 * a["electricity_usd_per_kwh"] + a["desktop_share_usd_per_hour"])
    cloud_hour = a["cloud_gpu_usd_per_hour"] + a["desktop_share_usd_per_hour"]
    successful = quality["successful"]

    def per(total: float) -> float | None:
        return round(total / successful, 4) if successful else None

    review = a["review_usd_per_case"] * quality["recorded"]
    tokens = (metrics.get("input_tokens") or 0) + (metrics.get("output_tokens") or 0)
    return {"runtime_hours": round(hours, 4), "successful_per_hour": round(successful / hours, 1) if hours else None,
            "on_prem_usd_per_hour": round(on_prem_hour, 4), "cloud_usd_per_hour": round(cloud_hour, 4),
            "on_prem_usd_per_success": per(on_prem_hour * hours), "cloud_usd_per_success": per(cloud_hour * hours),
            "review_usd_per_success": per(review) if review else None,
            "tokens_per_success": round(tokens / successful) if successful and tokens else None,
            "mean_power_w": round(power, 1)}


def markdown(runs: list[dict[str, Any]], a: dict[str, float]) -> str:
    rows = [("users", lambda r: r["meta"]["users"]), ("model", lambda r: r["meta"].get("model")),
            ("duration (s)", lambda r: r["meta"]["duration_seconds"]),
            ("samples / final / pending", lambda r: f"{r['quality']['samples']} / {r['quality']['final']} / {r['quality']['pending']}"),
            ("successful resolutions", lambda r: r["quality"]["successful"]),
            ("failed checks / failed labels", lambda r: f"{r['quality']['failed_checks']} / {r['quality']['failed_labels']}"),
            ("successful per hour", lambda r: r["cost"]["successful_per_hour"]),
            ("workflow p95 (s)", lambda r: r["metrics"]["workflow_p95_s"]),
            ("vLLM max running", lambda r: r["metrics"]["vllm_max_running"]),
            ("GPU utilization (mean)", lambda r: r["metrics"]["gpu_utilization"]),
            ("GPU power (mean W)", lambda r: r["metrics"]["gpu_power_w"]),
            ("tokens per successful resolution", lambda r: r["cost"]["tokens_per_success"]),
            ("on-prem $/hour", lambda r: r["cost"]["on_prem_usd_per_hour"]),
            ("on-prem $ per successful resolution", lambda r: r["cost"]["on_prem_usd_per_success"]),
            ("cloud $/hour", lambda r: r["cost"]["cloud_usd_per_hour"]),
            ("cloud $ per successful resolution", lambda r: r["cost"]["cloud_usd_per_success"]),
            ("reviewer $ per successful resolution", lambda r: r["cost"]["review_usd_per_success"])]
    lines = ["# Cost per successful resolution", "", "| Measure | " + " | ".join(r["label"] for r in runs) + " |",
             "| --- |" + " --- |" * len(runs)]
    lines += [f"| {name} | " + " | ".join(str(fn(r)) for r in runs) + " |" for name, fn in rows]
    lines += ["", "## Assumptions", ""] + [f"- `{k}`: {v}" for k, v in a.items()]
    lines += ["", "On-prem $/hour = host price / (years x 8760) + (mean GPU power + host overhead) x electricity",
              "+ desktop share. The hardware cost accrues whether or not the GPU is busy, so more successful",
              "resolutions per hour lowers the cost of each one. A resolution is successful when it was recorded",
              "(or correctly clarified) and passed the deterministic checks and, for labeled synthetic cases, the",
              "label. Samples still pending review at the end of the window are not counted.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("labels", nargs="+", help="load run labels (app mode)")
    parser.add_argument("--prometheus-url", default=os.environ.get("PROMETHEUS_URL", "http://prometheus:9090"))
    args = parser.parse_args()
    if not QUALITY_DB.exists():
        sys.exit(f"{QUALITY_DB} does not exist: run the load with QUALITY_SAMPLE_RATE > 0")
    store = QualityStore(QUALITY_DB)
    score_pending(store)
    a = assumptions()
    runs = []
    for label in args.labels:
        meta = find_meta(label)
        if meta["mode"] != "app":
            sys.exit(f"{label} is a {meta['mode']} run; cost per resolution needs an app run")
        metrics = window_metrics(args.prometheus_url, meta)
        quality = quality_in_window(store, meta)
        runs.append({"label": label, "meta": {k: meta.get(k) for k in ("users", "duration_seconds", "model", "started_at",
                                                                      "ended_at", "seed", "dataset_version", "_file")},
                     "metrics": metrics, "quality": quality, "cost": cost(meta, metrics, quality, a)})
    out = RESULTS_ROOT / "cost" / f"cost-{'-vs-'.join(safe_slug(l) for l in args.labels)}-{timestamp_slug()}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps({"kind": "cost", "assumptions": a, "runs": runs}, indent=2) + "\n",
                                        encoding="utf-8")
    out.with_suffix(".md").write_text(markdown(runs, a), encoding="utf-8")
    print(markdown(runs, a))
    print(f"wrote {out.name}.json and .md")
    return 0


if __name__ == "__main__":
    sys.exit(main())

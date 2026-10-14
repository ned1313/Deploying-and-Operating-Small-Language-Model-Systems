#!/usr/bin/env python3
"""Demo 1.3: compare load_sweep.py runs and export every recorded field to CSV.

Usage:
  compare_load.py 'results/load-*.json'
  compare_load.py --csv results/tuning.csv results/load-*-baseline-*.json results/load-*-kv-fp8-*.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

from taco_shared.paths import DEFAULT_RESULTS_DIR
from taco_shared.reporting import expand_globs, print_table, timestamp_slug

# (CSV column, console header or None to keep it CSV-only, getter)
RUN_FIELDS: list[tuple[str, str | None, Any]] = [
    ("file", None, lambda r, l: r["_path"].name),
    ("run_timestamp", None, lambda r, l: r["_timestamp"]),
    ("model", None, lambda r, l: r.get("model")),
    ("label", "label", lambda r, l: r.get("label") or "-"),
    ("max_tokens", None, lambda r, l: r.get("max_tokens")),
    ("pad_prompt_tokens", None, lambda r, l: r.get("pad_prompt_tokens")),
    ("structured_output", None, lambda r, l: r.get("structured_output")),
    ("requests_per_level", None, lambda r, l: r.get("requests_per_level")),
    ("extra_body", None, lambda r, l: json.dumps(r.get("extra_body") or {})),
]
LEVEL_FIELDS: list[tuple[str, str | None, Any]] = [
    ("concurrency", "conc", lambda r, l: l["concurrency"]),
    ("requests", "req", lambda r, l: l["requests"]),
    ("errors", "err", lambda r, l: l["errors"]),
    ("wall_seconds", "wall s", lambda r, l: l["wall_seconds"]),
    ("requests_per_second", "req/s", lambda r, l: l["requests_per_second"]),
    ("output_tokens_per_second", "out tok/s", lambda r, l: l["output_tokens_per_second"]),
    ("mean_prompt_tokens", "in tok", lambda r, l: l["mean_prompt_tokens"]),
    ("mean_completion_tokens", "out tok", lambda r, l: l["mean_completion_tokens"]),
    ("latency_mean_s", "lat avg", lambda r, l: l["latency_seconds"]["mean"]),
    ("latency_p50_s", "lat p50", lambda r, l: l["latency_seconds"]["p50"]),
    ("latency_p95_s", "lat p95", lambda r, l: l["latency_seconds"]["p95"]),
    ("latency_max_s", "lat max", lambda r, l: l["latency_seconds"]["max"]),
    ("ttft_mean_s", "ttft avg", lambda r, l: l["ttft_seconds"]["mean"]),
    ("ttft_p50_s", "ttft p50", lambda r, l: l["ttft_seconds"]["p50"]),
    ("ttft_p95_s", "ttft p95", lambda r, l: l["ttft_seconds"]["p95"]),
    ("ttft_max_s", "ttft max", lambda r, l: l["ttft_seconds"]["max"]),
    ("peak_running", "run", lambda r, l: l["vllm"]["peak_running"]),
    ("peak_waiting", "wait", lambda r, l: l["vllm"]["peak_waiting"]),
    ("peak_kv_cache_usage_pct", "kv %", lambda r, l: l["vllm"]["peak_kv_cache_usage_pct"]),
    ("preemptions_delta", "preempt", lambda r, l: l["vllm"]["preemptions_delta"]),
    ("metrics_samples", "samples", lambda r, l: l["vllm"]["metrics_samples"]),
    ("first_error", None, lambda r, l: (l.get("error_samples") or [{}])[0].get("message")),
]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="+", type=Path, help="load-*.json files written by load_sweep.py (globs allowed)")
    parser.add_argument("--csv", type=Path, default=None,
                        help="CSV output path (default: results/load-comparison-<timestamp>.csv).")
    return parser.parse_args()


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_path"] = path
    match = re.search(r"(\d{8}-\d{6})\.json$", path.name)
    data["_timestamp"] = match.group(1) if match else ""
    return data


def csv_value(value: Any) -> Any:
    return round(value, 4) if isinstance(value, float) else ("" if value is None else value)


def main() -> int:
    args = parse_arguments()
    runs = sorted((load(path) for path in expand_globs(args.results)), key=lambda r: r["_timestamp"])
    if not runs:
        print("No result files matched.", file=sys.stderr)
        return 1

    print("Runs")
    print_table(
        ["label", "model", "max_tokens", "pad", "structured", "req/level", "extra_body", "file"],
        [[r.get("label") or "-", r.get("model"), r.get("max_tokens"), r.get("pad_prompt_tokens"),
          r.get("structured_output"), r.get("requests_per_level"), json.dumps(r.get("extra_body") or {}),
          r["_path"].name] for r in runs],
    )

    console = [(header, get) for _, header, get in RUN_FIELDS + LEVEL_FIELDS if header]
    rows = [[get(r, level) for _, get in console] for r in runs for level in r["levels"]]
    print("\nLevels (latency and ttft in seconds; token columns are per-request means)")
    print_table([header for header, _ in console], rows)

    output = args.csv or DEFAULT_RESULTS_DIR / f"load-comparison-{timestamp_slug()}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = RUN_FIELDS + LEVEL_FIELDS
    with output.open("w", encoding="utf-8", newline="") as target:
        writer = csv.writer(target)
        writer.writerow([name for name, _, _ in fields])
        for r in runs:
            for level in r["levels"]:
                writer.writerow([csv_value(get(r, level)) for _, _, get in fields])
    print(f"\nCSV with {sum(len(r['levels']) for r in runs)} rows and {len(fields)} columns written to: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

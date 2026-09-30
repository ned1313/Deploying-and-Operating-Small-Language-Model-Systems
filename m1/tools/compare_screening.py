#!/usr/bin/env python3
"""Demo 1.2: compare screening results across model candidates or serving configurations.

Usage:
  compare_screening.py results/screening-*.json
  compare_screening.py --by-scenario results/screening-*.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from common import expand_globs, print_table


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="+", type=Path, help="screening-*.json files written by screen_models.py")
    parser.add_argument("--by-scenario", action="store_true", help="Also print a pass/fail grid per scenario.")
    return parser.parse_args()


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_path"] = path
    return data


def run_name(result: dict[str, Any]) -> str:
    name = result["model"]
    if result.get("label"):
        name += f" [{result['label']}]"
    if result.get("structured_output") and result["structured_output"] != "none":
        name += f" ({result['structured_output']})"
    return name


def pct(value: float | None) -> str | None:
    return None if value is None else f"{value * 100:.0f}%"


def main() -> int:
    args = parse_arguments()
    results = [load(path) for path in expand_globs(args.results)]
    if not results:
        print("No result files supplied.", file=sys.stderr)
        return 1
    results.sort(key=lambda r: -(r.get("weighted_score") or 0.0))

    headers = ["run", "pass", "schema", "strict", "grounded", "policy", "category", "score",
               "lat p50 s", "lat p95 s", "out tok", "gen tok/s", "peak VRAM MiB", "errors"]
    rows = []
    for r in results:
        rates, lat, tok, vram = r["rates"], r["latency_seconds"], r["tokens"], r.get("vram")
        rows.append([
            run_name(r), pct(rates["passed"]), pct(rates["schema_valid"]), pct(rates.get("strict_json")),
            pct(rates["grounded"]), pct(rates["policy_compliant"]), pct(rates["category_correct"]),
            f"{r['weighted_score']:.3f}" if r.get("weighted_score") is not None else None,
            lat["p50"], lat["p95"],
            f"{tok['completion_mean']:.0f}" if tok.get("completion_mean") is not None else None,
            tok.get("completion_tokens_per_second"),
            f"{vram['peak_memory_used_mib']:.0f}" if vram else None,
            r["api_errors"],
        ])
    print_table(headers, rows)
    print("\nscore = 0.35*schema + 0.25*grounded + 0.30*policy + 0.10*category (rates over scored scenarios).")
    print("Screening only: 20 scenarios, single run, no tool use. Confirm tool calling in module 2 before finalizing.")

    if args.by_scenario:
        scenario_ids = sorted({row["scenario_id"] for r in results for row in r["rows"]})
        labels = {row["scenario_id"]: row["label"] for r in results for row in r["rows"]}
        grid_headers = ["scenario", "label", *[run_name(r)[:28] for r in results]]
        grid = []
        for sid in scenario_ids:
            line: list[Any] = [sid, labels[sid][:36]]
            for r in results:
                row = next((x for x in r["rows"] if x["scenario_id"] == sid), None)
                if row is None:
                    line.append("-")
                elif row.get("api_error"):
                    line.append("ERR")
                else:
                    g = row["grades"]
                    line.append("PASS" if g["passed"] else
                                "".join(("S" if not g["schema_valid"] else "", "G" if not g["grounded"] else "",
                                         "P" if not g["policy_compliant"] else "", "C" if not g["category_correct"] else "")))
            grid.append(line)
        print()
        print_table(grid_headers, grid)
        print("\nFailure codes: S=schema, G=grounding, P=policy, C=category. Empty means passed sub-checks but still failed overall.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

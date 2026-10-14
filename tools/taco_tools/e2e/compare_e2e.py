#!/usr/bin/env python3
"""Demo 3.2: compare module 1 screening, module 2 agent, and module 3 end-to-end runs per scenario.

Usage:
  compare_e2e.py SCREENING_JSON AGENT_JSON E2E_JSON
  compare_e2e.py 'results/screening/screening-qwen3-14b-*.json' 'results/agent/agent-qwen3-14b-*.json' 'results/e2e/e2e-qwen3-14b-*.json'

Quoted globs are expanded here; the newest match is used.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from taco_shared.reporting import expand_globs, print_table


def newest(pattern: Path) -> Path:
    matches = expand_globs([pattern])
    if not matches:
        raise FileNotFoundError(f"no file matches {pattern}")
    return max(matches, key=lambda p: p.stat().st_mtime)


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def outcome(row: dict[str, Any] | None) -> str:
    if row is None:
        return "-"
    if row.get("api_error"):
        return "ERR"
    grades = row["grades"]
    codes = "".join(code for code, key in (("S", "schema_valid"), ("G", "grounded"), ("P", "policy_compliant"),
                                           ("C", "category_correct")) if not grades[key])
    return "PASS" if grades["passed"] else f"FAIL {codes}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("screening", type=Path)
    parser.add_argument("agent", type=Path)
    parser.add_argument("e2e", type=Path)
    args = parser.parse_args()
    paths = [newest(args.screening), newest(args.agent), newest(args.e2e)]
    screening, agent, e2e = (json.loads(p.read_text(encoding="utf-8")) for p in paths)
    if agent.get("kind") != "agent-eval" or e2e.get("kind") != "e2e-eval":
        print("Expected an m2 agent-eval result and an m3 e2e-eval result.", file=sys.stderr)
        return 2
    versions = {screening.get("dataset_version"), agent.get("screening_dataset_version"), e2e.get("screening_dataset_version")}
    if len(versions) != 1:
        print(f"Dataset mismatch across runs: {sorted(map(str, versions))}. Rerun on the current dataset.", file=sys.stderr)
        return 2
    for name, path, run in zip(("m1 screening", "m2 agent", "m3 e2e"), paths, (screening, agent, e2e)):
        print(f"{name + ':':<14}{path.name}  model={run['model']} label={run.get('label')}")
    if len({screening["model"], agent["model"], e2e["model"]}) > 1:
        print("Note: the runs used different models.")

    print()
    rates = [screening["rates"], agent["rates"], e2e["rates"]]
    print_table(["metric", "m1 screening", "m2 agent", "m3 e2e"],
                [[key, *(pct(r.get(key)) for r in rates)]
                 for key in ("passed", "schema_valid", "grounded", "policy_compliant", "category_correct", "proposal_accepted")])
    print("  m3 outcomes: " + "  ".join(f"{k}={v}" for k, v in e2e["outcomes"].items())
          + f"  duplicate_free={pct(e2e['rates'].get('duplicate_free'))}")

    by_id = [{r["scenario_id"]: r for r in run["rows"]} for run in (screening, agent, e2e)]
    grid = []
    for scenario_id in sorted(set().union(*by_id)):
        rows = [index.get(scenario_id) for index in by_id]
        label = next(r for r in rows if r)["label"]
        e2e_row = rows[2]
        workflow = "-" if e2e_row is None or e2e_row.get("api_error") else e2e_row["status"] + (
            f" ({e2e_row['outcome_reason']})" if e2e_row.get("outcome_reason") else "")
        grid.append([scenario_id, label, *(outcome(r) for r in rows), workflow])
    print()
    print_table(["scenario", "label", "m1 screening", "m2 agent", "m3 e2e", "m3 workflow"], grid)
    print("\nGrade codes: S=schema, G=grounding, P=policy, C=category. m3 grades the final proposal after any "
          "nudge or correction; escalated cases without a valid proposal fail grading.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

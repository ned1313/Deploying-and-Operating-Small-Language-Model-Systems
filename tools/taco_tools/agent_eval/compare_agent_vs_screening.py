#!/usr/bin/env python3
"""Demo 2.1: compare a module 1 screening run with a module 2 agent run, scenario by scenario.

Usage:
  compare_agent_vs_screening.py SCREENING_JSON AGENT_JSON
  compare_agent_vs_screening.py '/m1-results/screening-llama-3.1-8b-*.json' 'results/agent-llama-3.1-8b-*.json'

Quoted globs are expanded here; the newest match is used.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from taco_shared.reporting import expand_globs, print_table


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("screening", type=Path, help="screening-*.json written by m1 screen_models.py")
    parser.add_argument("agent", type=Path, help="agent-*.json written by m2 run_agent_scenarios.py")
    return parser.parse_args()


def newest(pattern: Path) -> Path:
    matches = expand_globs([pattern])
    if not matches:
        raise FileNotFoundError(f"no file matches {pattern}")
    return max(matches, key=lambda p: p.stat().st_mtime)


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def grade_codes(grades: dict[str, Any]) -> str:
    return "".join(code for code, key in (("S", "schema_valid"), ("G", "grounded"), ("P", "policy_compliant"),
                                          ("C", "category_correct")) if not grades[key])


def tool_codes(tools: dict[str, Any], accepted: bool) -> str:
    return "".join(code for code, ok in (("T", tools["tool_calls_parsed"]), ("A", tools["args_correct"]),
                                         ("R", tools["required_tools_called"]), ("B", tools["within_budget"]),
                                         ("V", accepted)) if not ok)


def main() -> int:
    args = parse_arguments()
    screening_path, agent_path = newest(args.screening), newest(args.agent)
    screening = json.loads(screening_path.read_text(encoding="utf-8"))
    agent = json.loads(agent_path.read_text(encoding="utf-8"))
    if agent.get("kind") != "agent-eval":
        print(f"{agent_path} is not an agent evaluation result.", file=sys.stderr)
        return 2
    if screening.get("dataset_version") != agent.get("screening_dataset_version"):
        print(f"Dataset mismatch: screening run used {screening.get('dataset_version')!r} but the agent run was built "
              f"with {agent.get('screening_dataset_version')!r}. Rerun the screening on the current dataset.",
              file=sys.stderr)
        return 2
    print(f"screening: {screening_path.name}  model={screening['model']} label={screening.get('label')}")
    print(f"agent:     {agent_path.name}  model={agent['model']} label={agent.get('label')}")
    if screening["model"] != agent["model"]:
        print("Note: the two runs used different models.")

    s_rates, a_rates = screening["rates"], agent["rates"]
    print()
    print_table(["metric", "m1 screening", "m2 agent", "delta"], [
        [key, pct(s_rates.get(key)), pct(a_rates.get(key)),
         "-" if s_rates.get(key) is None or a_rates.get(key) is None else f"{(a_rates[key] - s_rates[key]) * 100:+.0f} pts"]
        for key in ("passed", "schema_valid", "grounded", "policy_compliant", "category_correct")
    ])
    print()
    print_table(["agent metric", "rate"], [[key, pct(a_rates.get(key))] for key in (
        "tool_calls_parsed", "tool_args_valid", "required_tools_called", "args_correct", "within_budget",
        "proposal_accepted")])

    s_rows = {r["scenario_id"]: r for r in screening["rows"]}
    a_rows = {r["scenario_id"]: r for r in agent["rows"]}
    grid = []
    for scenario_id in sorted(set(s_rows) | set(a_rows)):
        s_row, a_row = s_rows.get(scenario_id), a_rows.get(scenario_id)
        label = (a_row or s_row)["label"]
        m1 = "-" if s_row is None else ("ERR" if s_row.get("api_error") else
                                        ("PASS" if s_row["grades"]["passed"] else "FAIL " + grade_codes(s_row["grades"])))
        if a_row is None:
            m2, tools = "-", "-"
        elif a_row.get("api_error"):
            m2, tools = "ERR", "-"
        else:
            m2 = "PASS" if a_row["grades"]["passed"] else "FAIL " + grade_codes(a_row["grades"])
            tools = tool_codes(a_row["tools"], a_row["validation"]["accepted"]) or "ok"
        grid.append([scenario_id, label, m1, m2, tools])
    print()
    print_table(["scenario", "label", "m1 screening", "m2 agent", "agent checks"], grid)
    print("\nGrade codes: S=schema, G=grounding, P=policy, C=category. Agent check codes: T=tool calls not parsed, "
          "A=wrong get_order arguments, R=required tool missing, B=tool budget hit, V=validator rejected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Demo 2.1: run the curated scenarios in agent form and grade tool calling plus the final proposal.

Confirms tool-calling reliability for the model selected in module 1 before the selection is final.
Each scenario is graded on: tool calls parsed by the backend, tool arguments valid, required tools
called, get_order called with the quoted order id, finished within the tool-call budget, proposal
accepted by the validator, and the shared grade() checks (schema, grounding, policy, category).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from openai import APIError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.loop import InvestigationResult, run_investigation  # noqa: E402
from agent.scenarios import build_tools, context_for, load_scenarios  # noqa: E402
from taco_shared.agent.model import build_chat_model  # noqa: E402
from taco_shared.config import InferenceSettings  # noqa: E402
from taco_shared.errors import describe_error  # noqa: E402
from taco_shared.grading import grade  # noqa: E402
from taco_shared.paths import AGENT_SCENARIOS, GENERATED_DIR, load_json  # noqa: E402
from taco_shared.reporting import safe_slug, summarize_latency, timestamp_slug  # noqa: E402

TOOL_FLAGS = ("tool_calls_parsed", "tool_args_valid", "required_tools_called", "args_correct", "within_budget")
GRADE_FLAGS = ("schema_valid", "grounded", "policy_compliant", "category_correct", "passed")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenarios", type=Path, default=AGENT_SCENARIOS)
    parser.add_argument("--only", help="Comma-separated scenario ids, e.g. S01,A01.")
    parser.add_argument("--limit", type=int, help="Only run the first N scenarios.")
    parser.add_argument("--actor", help="Override every scenario's actor (default: the scenario's own actor).")
    parser.add_argument("--no-structured-output", action="store_true", help="Finalize prompt-only.")
    parser.add_argument("--label", help="Free-text label for this run.")
    parser.add_argument("--include-full-responses", action="store_true", help="Store full transcripts in the results.")
    parser.add_argument("--output", type=Path, help="Results path (default: results/agent-<model>[-label]-<ts>.json).")
    return parser.parse_args()


def grade_tools(result: InvestigationResult, scenario: dict[str, Any]) -> dict[str, Any]:
    transcript = result.transcript
    model_steps = [s for s in transcript.steps if s["kind"] == "model"]
    calls = transcript.tool_calls
    names = [call["name"] for call in calls]
    expected = scenario["expected_tools"]
    signatures = [json.dumps([c["name"], c["args"]], sort_keys=True) for c in calls]
    return {
        "tool_calls_parsed": all(not s["invalid_tool_calls"] and not s["tool_markup_in_content"] for s in model_steps),
        "tool_args_valid": all(s["error_kind"] != "invalid_arguments" for s in transcript.tool_results),
        "required_tools_called": all(name in names for name in expected["required"]),
        "args_correct": any(c["name"] == "get_order" and c["args"].get("order_id") == expected["order_id"] for c in calls),
        "within_budget": not transcript.limit_hit,
        "tool_call_count": len(calls),
        "repeated_calls": len(signatures) - len(set(signatures)),
        "invalid_tool_call_turns": sum(1 for s in model_steps if s["invalid_tool_calls"]),
        "tool_errors": [s["error_kind"] for s in transcript.tool_results if s["status"] != "ok"],
        "tools_called": names,
    }


def run_scenario(settings: InferenceSettings, scenario: dict[str, Any], orders: dict[str, Any],
                 actor: str | None, keep_transcript: bool) -> dict[str, Any]:
    row: dict[str, Any] = {"scenario_id": scenario["scenario_id"], "label": scenario["label"]}
    ctx = context_for(scenario["intake"], actor or scenario["actor_id"])
    started = time.perf_counter()
    try:
        result = run_investigation(build_chat_model(settings), build_tools(settings, ctx), scenario["intake"], settings)
    except APIError as error:
        row.update(latency_seconds=time.perf_counter() - started, api_error=describe_error(error))
        return row
    model_steps = [s for s in result.transcript.steps if s["kind"] == "model"] + [result.finalize_step]
    report = result.report
    order = orders[scenario["expected_tools"]["order_id"]]
    graded = grade(result.proposal, {"expected": scenario["expected"], "order": order}, report.schema_errors)
    row.update(
        api_error=None, latency_seconds=result.elapsed_seconds,
        model_calls=len(model_steps),
        model_call_seconds=[s["elapsed_seconds"] for s in model_steps],
        input_tokens=sum(s.get("input_tokens") or 0 for s in model_steps),
        output_tokens=sum(s.get("output_tokens") or 0 for s in model_steps),
        tools=grade_tools(result, scenario),
        validation=report.to_dict(),
        grades=graded,
        proposal=result.proposal,
    )
    if keep_transcript:
        row["run"] = result.to_dict()
    return row


def flag(value: bool) -> str:
    return "Y" if value else "n"


def main() -> int:
    args = parse_arguments()
    settings = InferenceSettings.from_env()
    if args.no_structured_output:
        settings = replace(settings, structured_output="none")
    dataset = load_scenarios(args.scenarios)
    scenarios = dataset["scenarios"]
    if args.only:
        wanted = {item.strip() for item in args.only.split(",")}
        scenarios = [s for s in scenarios if s["scenario_id"] in wanted]
    if args.limit:
        scenarios = scenarios[: args.limit]
    orders = {order["order_id"]: order for order in load_json(GENERATED_DIR / "orders.json")}
    report = load_json(GENERATED_DIR / "build_report.json")

    print(f"Agent evaluation: {settings.model} on {len(scenarios)} scenarios "
          f"(structured_output={settings.structured_output}, max_tool_calls={settings.max_tool_calls})")
    started = time.perf_counter()
    rows = []
    for scenario in scenarios:
        row = run_scenario(settings, scenario, orders, args.actor, args.include_full_responses)
        rows.append(row)
        if row.get("api_error"):
            error = row["api_error"]
            print(f"  {row['scenario_id']:>4} {row['label']:<38} ERROR {error['kind']} HTTP {error['status_code']}: {error['message'][:100]}")
            continue
        tools, grades = row["tools"], row["grades"]
        print(f"  {row['scenario_id']:>4} {row['label']:<38} {'PASS' if grades['passed'] else 'FAIL'} "
              f"parsed={flag(tools['tool_calls_parsed'])} args={flag(tools['args_correct'])} "
              f"required={flag(tools['required_tools_called'])} budget={flag(tools['within_budget'])} "
              f"valid={flag(row['validation']['accepted'])} grounded={flag(grades['grounded'])} "
              f"policy={flag(grades['policy_compliant'])} {row['latency_seconds']:5.1f}s calls={tools['tool_call_count']}")
    elapsed = time.perf_counter() - started

    scored = [r for r in rows if not r.get("api_error")]

    def rate(values: list[bool]) -> float | None:
        return statistics.mean(1.0 if v else 0.0 for v in values) if values else None

    rates = {key: rate([r["tools"][key] for r in scored]) for key in TOOL_FLAGS}
    rates["proposal_accepted"] = rate([r["validation"]["accepted"] for r in scored])
    rates.update({key: rate([r["grades"][key] for r in scored]) for key in GRADE_FLAGS})
    summary = {
        "kind": "agent-eval", "model": settings.model, "label": args.label,
        "dataset_version": dataset.get("dataset_version"), "policy_version": dataset.get("policy_version"),
        "screening_dataset_version": report["screening_version"], "settings": settings.describe(),
        "scenarios": len(rows), "scored": len(scored), "api_errors": len(rows) - len(scored),
        "rates": rates, "latency_seconds": summarize_latency([r["latency_seconds"] for r in scored]),
        "wall_seconds": elapsed,
        "tokens": {"input_mean": statistics.mean(r["input_tokens"] for r in scored) if scored else None,
                   "output_mean": statistics.mean(r["output_tokens"] for r in scored) if scored else None},
        "model_calls_mean": statistics.mean(r["model_calls"] for r in scored) if scored else None,
        "tool_calls_mean": statistics.mean(r["tools"]["tool_call_count"] for r in scored) if scored else None,
        "rows": rows,
    }
    output = args.output or (Path(settings.results_dir) / f"agent-{safe_slug(settings.model)}"
                             f"{'-' + safe_slug(args.label) if args.label else ''}-{timestamp_slug()}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print("\nAgent evaluation summary")
    for key, value in rates.items():
        print(f"  {key + ':':<24}{'-' if value is None else f'{value:.2f}'}")
    lat = summary["latency_seconds"]
    if lat["mean"] is not None:
        print(f"  latency s:              mean={lat['mean']:.2f} p50={lat['p50']:.2f} p95={lat['p95']:.2f}")
        print(f"  per scenario:           model calls={summary['model_calls_mean']:.1f} tool calls={summary['tool_calls_mean']:.1f} "
              f"input tok={summary['tokens']['input_mean']:.0f} output tok={summary['tokens']['output_mean']:.0f}")
    print(f"\nDetailed results written to: {output}")
    return 1 if summary["api_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())

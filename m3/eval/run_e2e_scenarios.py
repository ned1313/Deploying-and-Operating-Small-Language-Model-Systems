#!/usr/bin/env python3
"""Demo 3.2: run the labeled scenarios end to end through the complaint API.

For each labeled scenario (S01-S22, A01, A02): reset its case, submit it as the scenario's service rep,
approve the proposal as the area manager when it reaches review, then check that a repeated approval
and a resubmission do not record a second action. The final proposal is graded with the same grade()
used by modules 1 and 2, so the results can be compared with compare_e2e.py.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients.api import Api  # noqa: E402
from taco_shared.grading import grade  # noqa: E402
from taco_shared.paths import AGENT_SCENARIOS_V2, GENERATED_DIR, load_json  # noqa: E402
from taco_shared.reporting import safe_slug, summarize_latency, timestamp_slug  # noqa: E402

APPROVER = "sup-area-01"
GRADE_FLAGS = ("schema_valid", "grounded", "policy_compliant", "category_correct", "passed")
OUTCOMES = ("recorded", "escalated", "needs_clarification", "failed_retryable", "rejected")


def run_scenario(api: Api, scenario: dict[str, Any], orders: dict[str, Any]) -> dict[str, Any]:
    case_id = scenario["intake"]["case_id"]
    api.reset([case_id])
    started = time.perf_counter()
    response = api.submit(scenario["actor_id"], scenario["scenario_id"])
    processing = time.perf_counter() - started
    row: dict[str, Any] = {"scenario_id": scenario["scenario_id"], "label": scenario["label"],
                           "latency_seconds": processing}
    if response.status_code >= 400:
        row.update(api_error={"status_code": response.status_code, "detail": response.text[:300]})
        return row
    view = response.json()
    reached_review = view["status"] == "pending_review"
    duplicate_free = True
    if reached_review:
        decided = api.decide(APPROVER, case_id, "approve", view["proposal_version"]).json()
        repeat = api.decide(APPROVER, case_id, "approve", view["proposal_version"]).json()
        api.submit(scenario["actor_id"], scenario["scenario_id"])
        view = decided
        duplicate_free = repeat.get("replayed") is True
    actions = api.approved_actions(case_id, view["store_id"])
    duplicate_free = duplicate_free and len(actions) <= 1
    validation = view.get("validation") or {"accepted": False, "schema_errors": ["no proposal"], "business_rule_violations": []}
    order = orders[scenario["expected_tools"]["order_id"]]
    graded = grade(view.get("proposal"), {"expected": scenario["expected"], "order": order}, validation["schema_errors"])
    row.update(api_error=None, status=view["status"], outcome_reason=view.get("outcome_reason"),
               reached_review=reached_review, recorded=view["status"] == "recorded",
               duplicate_free=duplicate_free, actions=len(actions), counters=view.get("counters", {}),
               validation=validation, grades=graded, proposal=view.get("proposal"))
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="Comma-separated scenario ids.")
    parser.add_argument("--label")
    parser.add_argument("--keep-state", action="store_true",
                        help="Leave the recorded cases in place (default: restore their fixtures afterwards).")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    api = Api()
    health = api.call("GET", "/healthz").json()
    model = health.get("model") or "unknown"
    dataset = load_json(AGENT_SCENARIOS_V2)
    scenarios = [s for s in dataset["scenarios"] if not s.get("workflow")]
    if args.only:
        wanted = {item.strip() for item in args.only.split(",")}
        scenarios = [s for s in scenarios if s["scenario_id"] in wanted]
    orders = {order["order_id"]: order for order in load_json(GENERATED_DIR / "orders.json")}
    report = load_json(GENERATED_DIR / "build_report.json")

    print(f"End-to-end evaluation through the complaint API: {model} on {len(scenarios)} scenarios (approver {APPROVER})")
    started = time.perf_counter()
    rows = []
    for scenario in scenarios:
        row = run_scenario(api, scenario, orders)
        rows.append(row)
        if row.get("api_error"):
            print(f"  {row['scenario_id']:>4} {row['label']:<38} ERROR HTTP {row['api_error']['status_code']}")
            continue
        c = row["counters"]
        print(f"  {row['scenario_id']:>4} {row['label']:<38} {'PASS' if row['grades']['passed'] else 'FAIL'} "
              f"{row['status']:<20} tools={c.get('tool_calls')} nudges={c.get('nudges')} fixes={c.get('corrections')} "
              f"actions={row['actions']} {row['latency_seconds']:5.1f}s"
              + (f"  ({row['outcome_reason']})" if row.get("outcome_reason") else ""))
    scored = [r for r in rows if not r.get("api_error")]
    if not args.keep_state:
        # The case API volume is shared with module 2; leftover actions would read as R13 there.
        api.reset([s["intake"]["case_id"] for s in scenarios])

    def rate(values: list[bool]) -> float | None:
        return statistics.mean(1.0 if v else 0.0 for v in values) if values else None

    rates = {key: rate([r["grades"][key] for r in scored]) for key in GRADE_FLAGS}
    rates.update(proposal_accepted=rate([r["validation"]["accepted"] for r in scored]),
                 reached_review=rate([r["reached_review"] for r in scored]),
                 duplicate_free=rate([r["duplicate_free"] for r in scored]))
    outcomes = {status: sum(1 for r in scored if r["status"] == status) for status in OUTCOMES}
    summary = {"kind": "e2e-eval", "model": model, "label": args.label,
               "dataset_version": dataset["dataset_version"], "policy_version": dataset["policy_version"],
               "screening_dataset_version": report["screening_version"], "agent_dataset_version": dataset["base_dataset_version"],
               "scenarios": len(rows), "scored": len(scored), "api_errors": len(rows) - len(scored),
               "rates": rates, "outcomes": outcomes,
               "latency_seconds": summarize_latency([r["latency_seconds"] for r in scored]),
               "wall_seconds": time.perf_counter() - started, "rows": rows}
    output = args.output or (Path("results") / f"e2e-{safe_slug(model)}{'-' + safe_slug(args.label) if args.label else ''}"
                                               f"-{timestamp_slug()}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print("\nEnd-to-end summary")
    for key, value in rates.items():
        print(f"  {key + ':':<22}{'-' if value is None else f'{value:.2f}'}")
    print("  outcomes:             " + "  ".join(f"{k}={v}" for k, v in outcomes.items()))
    lat = summary["latency_seconds"]
    if lat["mean"] is not None:
        print(f"  processing s:         mean={lat['mean']:.2f} p50={lat['p50']:.2f} p95={lat['p95']:.2f} (excludes approval)")
    print(f"\nDetailed results written to: {output}")
    return 1 if summary["api_errors"] or rates["duplicate_free"] not in (None, 1.0) else 0


if __name__ == "__main__":
    sys.exit(main())

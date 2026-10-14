#!/usr/bin/env python3
"""Demo 3.2: replay failure scenarios against the running complaint API and compare expected outcomes.

Each workflow scenario in agent-v2 (C*, J*, F*) is reset, its faults are applied through the admin
routes, it is submitted as its service rep, and the resulting workflow status is compared with the
expected one. Then the A01 approval path is checked for authorization and idempotency.
Nothing here approves a scenario's proposal except the explicit approval checks on A01.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients.api import Api  # noqa: E402
from taco_shared.paths import AGENT_SCENARIOS_V2, load_json  # noqa: E402
from taco_shared.reporting import print_table, timestamp_slug  # noqa: E402

REVIEWER, OTHER_STORE_REVIEWER = "sup-kop-01", "sup-wg-01"


def describe_faults(faults: list[dict[str, Any]]) -> str:
    parts = []
    for fault in faults:
        detail = {k: v for k, v in fault.items() if k not in {"target", "mode"}}
        parts.append(f"{fault['target']}:{fault['mode']}" + (f" {json.dumps(detail, separators=(',', ':'))}" if detail else ""))
    return "; ".join(parts) or "-"


def expected_text(workflow: dict[str, Any]) -> str:
    text = workflow["status"] + (f" ({workflow['reason']})" if workflow.get("reason") else "")
    return text + (f" -> {workflow['after']} -> {workflow['after_status']}" if workflow.get("after") else "")


def run_workflow_scenario(api: Api, scenario: dict[str, Any]) -> dict[str, Any]:
    case_id, actor, workflow = scenario["intake"]["case_id"], scenario["actor_id"], scenario["workflow"]
    api.reset([case_id])
    api.clear_faults()
    model_faults = [{k: v for k, v in f.items() if k != "target"} for f in scenario["faults"] if f["target"] == "model"]
    for fault in (f for f in scenario["faults"] if f["target"] == "case_api"):
        api.set_case_api_fault({"path_prefix": f"/cases/{case_id}", **{k: v for k, v in fault.items() if k != "target"}})
    if model_faults:
        api.set_model_faults(model_faults)
    started = time.perf_counter()
    notes: list[str] = []
    response = api.submit(actor, scenario["scenario_id"])
    view = response.json() if response.status_code < 500 else {"status": f"HTTP {response.status_code}"}
    actual = view.get("status", f"HTTP {response.status_code}")
    reason = view.get("outcome_reason") or ""
    ok = actual == workflow["status"] and reason.startswith(workflow.get("reason", ""))
    actual_text = actual + (f" ({reason})" if reason else "")
    if ok and workflow.get("after") == "clarify":
        view = api.call("POST", f"/cases/{case_id}/clarify", actor,
                        json={"answer": workflow["clarification_answer"]}).json()
        actual_text += f" -> clarify -> {view['status']}"
        ok = view["status"] == workflow["after_status"]
    elif ok and workflow.get("after") == "retry":
        api.clear_faults()
        view = api.call("POST", f"/cases/{case_id}/retry", actor).json()
        actual_text += f" -> retry -> {view['status']}"
        ok = view["status"] == workflow["after_status"]
    api.clear_faults()

    proposal = view.get("proposal") or {}
    if scenario["scenario_id"] == "J01" and proposal and float(proposal.get("refund_amount") or 0) > 27.84:
        ok, notes = False, notes + [f"refund {proposal.get('refund_amount')} exceeds the order total"]
    if scenario["scenario_id"] == "J03" and proposal and proposal.get("order_id") != scenario["expected_tools"]["order_id"]:
        ok, notes = False, notes + [f"proposal names order {proposal.get('order_id')}"]
    actions = len(api.approved_actions(case_id, view.get("store_id") or "KOP"))
    if actions:
        ok, notes = False, notes + ["an action was recorded without approval"]
    counters = view.get("counters", {})
    if view.get("injected_faults"):
        notes.append("injected: " + ",".join(view["injected_faults"]))
    if proposal and view.get("status") == "pending_review":
        notes.append(f"proposal {proposal.get('resolution_type')} {proposal.get('refund_amount')}")
    return {"id": scenario["scenario_id"], "label": scenario["label"], "fault": describe_faults(scenario["faults"]),
            "expected": expected_text(workflow), "actual": actual_text, "counters": counters, "actions": actions,
            "seconds": time.perf_counter() - started, "passed": ok, "notes": "; ".join(notes)}


def approval_checks(api: Api) -> list[dict[str, Any]]:
    case_id, rows = "CASE-10431", []
    api.reset([case_id])
    api.clear_faults()
    view = api.submit("rep-kop-01", "A01").json()

    def row(check_id: str, label: str, expected: str, actual: str, ok: bool) -> None:
        rows.append({"id": check_id, "label": label, "fault": "-", "expected": expected, "actual": actual,
                     "counters": {}, "actions": len(api.approved_actions(case_id, "KOP")), "seconds": 0.0,
                     "passed": ok, "notes": ""})

    if view.get("status") != "pending_review":
        row("D00", "a01_reaches_review", "pending_review", view.get("status", "?"), False)
        return rows
    version = view["proposal_version"]
    for check_id, label, actor, use_version, expected_code in (
            ("D01", "submitter_cannot_approve", "rep-kop-01", version, 403),
            ("D02", "other_store_reviewer_denied", OTHER_STORE_REVIEWER, version, 404),
            ("D03", "stale_version_rejected", REVIEWER, version + 1, 409)):
        code = api.decide(actor, case_id, "approve", use_version).status_code
        row(check_id, label, f"HTTP {expected_code}", f"HTTP {code}", code == expected_code)
    first = api.decide(REVIEWER, case_id, "approve", version).json()
    row("D04", "reviewer_approves_and_records", "recorded, 1 action", f"{first.get('status')}",
        first.get("status") == "recorded" and len(api.approved_actions(case_id, "KOP")) == 1)
    again = api.decide(REVIEWER, case_id, "approve", version).json()
    row("D05", "repeat_approval_is_replayed", "replayed, 1 action", f"replayed={again.get('replayed')}",
        again.get("replayed") is True and len(api.approved_actions(case_id, "KOP")) == 1)
    resubmit = api.submit("rep-kop-01", "A01")
    row("D06", "resubmission_is_replayed", "HTTP 200 replayed, 1 action",
        f"HTTP {resubmit.status_code} replayed={resubmit.json().get('replayed')}",
        resubmit.status_code == 200 and len(api.approved_actions(case_id, "KOP")) == 1)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="Comma-separated scenario ids, e.g. C01,F03 (D for the approval checks).")
    parser.add_argument("--label")
    parser.add_argument("--keep-state", action="store_true",
                        help="Leave the scenario cases as they ended (default: restore their fixtures afterwards).")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    api = Api()
    wanted = {item.strip() for item in args.only.split(",")} if args.only else None
    scenarios = [s for s in load_json(AGENT_SCENARIOS_V2)["scenarios"] if s.get("workflow")
                 and (wanted is None or s["scenario_id"] in wanted)]
    rows = []
    for scenario in scenarios:
        print(f"  running {scenario['scenario_id']} {scenario['label']} ...", flush=True)
        rows.append(run_workflow_scenario(api, scenario))
    if wanted is None or "D" in wanted:
        print("  running A01 approval and idempotency checks ...", flush=True)
        rows.extend(approval_checks(api))
    if not args.keep_state:
        # The case API volume is shared with module 2; leftover actions would read as R13 there.
        api.reset([s["intake"]["case_id"] for s in scenarios] + (["CASE-10431"] if wanted is None or "D" in wanted else []))

    print()
    print_table(["id", "label", "injected fault", "expected", "actual", "retries", "fixes", "repeats", "actions", "s", "result"], [
        [r["id"], r["label"], r["fault"][:42], r["expected"], r["actual"],
         r["counters"].get("case_api_retries", "-"), r["counters"].get("corrections", "-"),
         r["counters"].get("repeated_calls", "-"), r["actions"], f"{r['seconds']:.1f}", "PASS" if r["passed"] else "FAIL"]
        for r in rows])
    for r in rows:
        if r["notes"]:
            print(f"  {r['id']}: {r['notes']}")
    failed = [r["id"] for r in rows if not r["passed"]]
    print(f"\n{len(rows) - len(failed)}/{len(rows)} passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    output = args.output or Path("results") / f"failure-tests{'-' + args.label if args.label else ''}-{timestamp_slug()}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"kind": "failure-tests", "label": args.label, "rows": rows}, indent=2), encoding="utf-8")
    print(f"Results written to: {output}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

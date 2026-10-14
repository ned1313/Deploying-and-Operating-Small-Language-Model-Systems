#!/usr/bin/env python3
"""Command-line client for the complaint API (module 3).

  taco_cli.py submit A01                       submit a scenario as rep-kop-01
  taco_cli.py show CASE-10431 [--steps]        case status, proposal, validation, reviews, action
  taco_cli.py reviews                          pending reviews for the reviewer (default sup-kop-01)
  taco_cli.py approve CASE-10431               approve the current proposal version
  taco_cli.py revise CASE-10431 --note "..."   request a revision
  taco_cli.py reject CASE-10431
  taco_cli.py clarify CASE-10431-C01 --answer "The order number is TA-10431."
  taco_cli.py retry CASE-10431-F05
  taco_cli.py graph                            the workflow as a Mermaid diagram
  taco_cli.py reset CASE-10431 [...]           forget a case and restore its fixture (admin token)
  taco_cli.py case-fault latency --ms 2500     slow the case API (module 4); also: timeout, clear

--actor sets X-Actor-Id (an unauthenticated identity claim; see app/README.md).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from taco_shared.api_client import Api

REP = os.environ.get("TACO_ACTOR") or "rep-kop-01"
REVIEWER = os.environ.get("REVIEW_ACTOR") or "sup-kop-01"


def money(value: Any) -> str:
    return f"{float(value):.2f}" if isinstance(value, (int, float)) else str(value)


def render(view: dict[str, Any], steps: bool = False) -> None:
    reason = f" ({view['outcome_reason']})" if view.get("outcome_reason") else ""
    replay = "  [replayed: no new run]" if view.get("replayed") else ""
    print(f"Case {view['case_id']}  status: {view['status']}{reason}  store {view['store_id']}  "
          f"submitted by {view['submitted_by']}{replay}")
    print(f"  complaint: {view.get('complaint')}")
    if view.get("clarification_question"):
        print(f"  question for the customer: {view['clarification_question']}")
    proposal = view.get("proposal")
    if proposal:
        print(f"  proposal v{view['proposal_version']}: {proposal.get('resolution_type')} {money(proposal.get('refund_amount'))}"
              f"  order {proposal.get('order_id')}  items {proposal.get('affected_items')}  "
              f"rules {proposal.get('policy_rule_ids')}  approval flag {proposal.get('requires_human_approval')}")
        print(f"    customer message: {proposal.get('customer_message')}")
    validation = view.get("validation")
    if validation:
        problems = validation["schema_errors"] + validation["business_rule_violations"]
        print(f"  validation: {'accepted' if validation['accepted'] else 'REJECTED'}"
              + ("".join(f"\n    - {p}" for p in problems) if problems else ""))
    counters = view.get("counters", {})
    print("  counters: " + "  ".join(f"{k}={v}" for k, v in counters.items()))
    if view.get("injected_faults"):
        print(f"  injected faults: {view['injected_faults']}")
    for review in view.get("reviews", []):
        print(f"  review: v{review['proposal_version']} {review['decision']} by {review['reviewer']}"
              + (f" ({review['note']})" if review.get("note") else ""))
    if view.get("action"):
        action = view["action"]
        print(f"  recorded action: {action['action_id']} {action['type']} {money(action['amount'])} at {action['recorded_at']}")
    if steps:
        for index, step in enumerate(view.get("steps", []), start=1):
            print(f"  [{index:>2}] {json.dumps(step, default=str)[:220]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--actor", help="X-Actor-Id for the call (defaults depend on the command).")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("submit")
    p.add_argument("scenario")
    p.add_argument("--steps", action="store_true")
    p = sub.add_parser("show")
    p.add_argument("case_id")
    p.add_argument("--steps", action="store_true")
    sub.add_parser("list")
    sub.add_parser("reviews")
    for name in ("approve", "reject", "revise"):
        p = sub.add_parser(name)
        p.add_argument("case_id")
        p.add_argument("--version", type=int, help="Proposal version (default: the current one).")
        p.add_argument("--note")
    p = sub.add_parser("clarify")
    p.add_argument("case_id")
    p.add_argument("--answer", required=True)
    p = sub.add_parser("retry")
    p.add_argument("case_id")
    sub.add_parser("graph")
    p = sub.add_parser("reset")
    p.add_argument("case_ids", nargs="+")
    p = sub.add_parser("case-fault", help="inject or clear a case API fault (admin token)")
    p.add_argument("mode", choices=["latency", "timeout", "clear"])
    p.add_argument("--ms", type=int, default=2500, help="latency mode: added delay")
    p.add_argument("--after", type=int, default=1,
                   help="skip this many matching requests first (1 skips the API's own lookup at submit)")
    p.add_argument("--count", type=int, help="stop after this many faulted requests (default: until cleared)")
    args = parser.parse_args()
    api = Api()

    if args.command == "reset":
        api.reset(args.case_ids)
        print(f"Reset {', '.join(args.case_ids)} in the complaint API and the case API.")
        return 0
    if args.command == "case-fault":
        if args.mode == "clear":
            api.clear_faults()
            print("Cleared model and case API faults.")
            return 0
        fault = {"mode": args.mode, "after": args.after, "count": args.count,
                 **({"latency_ms": args.ms} if args.mode == "latency" else {"timeout_seconds": 10})}
        api.set_case_api_fault(fault)
        print(f"Case API fault set: {fault}")
        return 0

    reviewer_commands = {"reviews", "approve", "reject", "revise"}
    actor = args.actor or (REVIEWER if args.command in reviewer_commands else REP)
    command = args.command
    if command == "submit":
        response = api.submit(actor, args.scenario)
    elif command == "show":
        response = api.case(actor, args.case_id, steps=args.steps)
    elif command == "list":
        response = api.call("GET", "/cases", actor)
    elif command == "reviews":
        response = api.call("GET", "/reviews", actor)
    elif command in {"approve", "reject", "revise"}:
        version = args.version
        if version is None:
            response = api.case(actor, args.case_id)
            if response.status_code == 200:
                version = response.json()["proposal_version"]
        if version is not None:
            response = api.decide(actor, args.case_id, command, version, args.note)
    elif command == "clarify":
        response = api.call("POST", f"/cases/{args.case_id}/clarify", actor, json={"answer": args.answer})
    elif command == "retry":
        response = api.call("POST", f"/cases/{args.case_id}/retry", actor)
    else:
        response = api.call("GET", "/graph")
        print(response.text)
        return 0 if response.status_code == 200 else 1

    print(f"{response.request.method} {response.request.url.path} as {actor} -> HTTP {response.status_code}"
          f"  (request {response.headers.get('X-Request-ID', '-')})")
    if response.status_code >= 400:
        try:
            print(f"  {response.json().get('detail')}")
        except ValueError:
            print(f"  {response.text[:300]}")
        return 1
    body = response.json()
    if isinstance(body, list):
        for case in body:
            print(f"  {case['case_id']:<18} {case['status']:<20} store {case['store_id']}  v{case['proposal_version']}  "
                  f"by {case['submitted_by']}  {case['updated_at']}")
        if not body:
            print("  (none)")
    else:
        render(body, steps=getattr(args, "steps", False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

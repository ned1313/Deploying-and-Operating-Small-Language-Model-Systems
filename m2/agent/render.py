"""Step-by-step console output for the recording."""

from __future__ import annotations

import json
from typing import Any


def _tokens(step: dict[str, Any]) -> str:
    out = step.get("output_tokens")
    return f"{out} tok" if out is not None else ""


def summarize_tool_result(step: dict[str, Any]) -> str:
    result = step["result"]
    if step["status"] != "ok":
        return f"error {result.get('error_kind')}: {result.get('message')}"
    name = step["name"]
    if name == "get_order":
        order = result["order"]
        return f"ok {result['record_id']}  {len(order['items'])} items, total {order['total']:.2f}, store {order['store_id']}"
    if name == "get_prior_resolutions":
        return f"ok {len(result['resolutions'])} resolution(s) in the last {result['lookback_days']} days"
    if name == "get_resolution_policy":
        return f"ok policy {result['policy_version']}: {', '.join(rule['id'] for rule in result['rules'])}"
    if name == "get_case_status":
        case = result["case"]
        return f"ok {result['record_id']}  status {case['status']}, {len(case['recorded_actions'])} recorded action(s)"
    return f"ok {result.get('record_id')}"


class StepPrinter:
    def __init__(self, verbose: bool = False) -> None:
        self.count = 0
        self.verbose = verbose

    def __call__(self, step: dict[str, Any]) -> None:
        kind = step["kind"]
        if kind == "model":
            self.count += 1
            timing = f"{step['elapsed_seconds']:5.1f}s {_tokens(step)}"
            calls = step["tool_calls"]
            if calls:
                for call in calls:
                    head = f"[{self.count}] model -> tool_call {call['name']} {json.dumps(call['args'])}"
                    print(f"{head:<72} {timing}")
            else:
                text = " ".join(step["content"].split())
                print(f"{f'[{self.count}] model -> answer':<72} {timing}")
                print(f"    {text[:400]}{'...' if len(text) > 400 else ''}")
            for bad in step["invalid_tool_calls"]:
                print(f"    INVALID tool call {bad.get('name')}: {bad.get('error') or bad.get('args')}")
            if step["tool_markup_in_content"]:
                print("    WARNING: tool-call markup left in content (parser or chat-template mismatch)")
        elif kind == "tool":
            print(f"    tool   <- {step['name']}: {summarize_tool_result(step)}")
            if self.verbose:
                print(f"       {json.dumps(step['result'])}")
        elif kind == "limit":
            print(f"    LIMIT  {step['message']}")
        elif kind == "finalize":
            self.count += 1
            head = f"[{self.count}] finalize ({step['mode']}) -> ProposalV2"
            print(f"{head:<72} {step['elapsed_seconds']:5.1f}s {_tokens(step)}")
            if step.get("proposal") is not None:
                print(json.dumps(step["proposal"], indent=2))
            report = step["report"]
            verdict = "ACCEPTED (pending human approval in module 3)" if report.accepted else "REJECTED"
            print(f"    schema: {'valid' if not report.schema_errors else 'INVALID'}   "
                  f"business rules: {len(report.business_rule_violations)} violation(s)   {verdict}")
            for error in report.schema_errors:
                print(f"    schema error: {error}")
            for violation in report.business_rule_violations:
                print(f"    rule violation: {violation}")

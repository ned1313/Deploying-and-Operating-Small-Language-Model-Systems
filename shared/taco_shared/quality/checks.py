"""Deterministic quality checks on a sampled outcome, labeled grading, and "successful resolution".

The checks use only what the sample carries: the proposal and the tool results the agent retrieved. They
go further than `validate_proposal` (which every reviewed proposal already passed) by recomputing amounts,
approval flags, and rule fit from that evidence. They still cannot tell whether the complaint was
classified correctly; a label (synthetic data only) or a human reviewer catches that.
"""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from typing import Any

from ..grading import grade
from ..paths import AGENT_SCENARIOS_V2, GENERATED_DIR, load_json
from ..policy import Policy
from ..policy_engine import minutes_late, money, parse_date, recent_resolution_count
from ..proposal import (APPROVAL_THRESHOLD, FILING_WINDOW_DAYS, MONETARY_TYPES, Evidence, load_schema_v2,
                        schema_errors)

CHECKS = ("schema_valid", "order_grounded", "amount_grounded", "rules_consistent", "approval_flag",
          "prior_compensation", "filing_window", "duplicate_actions", "policy_version_current")
DEFECT_CATEGORIES = {"missing_items", "wrong_item", "late_delivery", "order_not_received", "wrong_address",
                     "food_quality"}
OVERRIDE_RULES = {"R12", "R13"}
CORRECT_CLARIFICATION = {"missing_order_id", "order_not_found"}
CENT = Decimal("0.01")


def evidence_of(sample: dict[str, Any]) -> Evidence:
    evidence = Evidence()
    for entry in sample.get("evidence", []):
        evidence.add(entry["tool"], entry["result"])
    return evidence


def _result(passed: bool | None, detail: str = "") -> dict[str, Any]:
    return {"passed": passed, "detail": detail}


def _amount(proposal: dict[str, Any]) -> Decimal | None:
    value = proposal.get("refund_amount")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return money(value)


def _compensated(evidence: Evidence, order_id: str | None) -> bool:
    prior = any(p.get("order_id") == order_id and (p.get("amount") or 0) > 0 for p in evidence.prior_resolutions or [])
    case = evidence.case or {}
    recorded = case.get("order_id") == order_id and any((a.get("amount") or 0) > 0
                                                        for a in case.get("recorded_actions", []))
    return prior or recorded


def _late_tier(order: dict[str, Any]) -> tuple[str, Decimal] | None:
    late = minutes_late(order)
    if late is None:
        return None
    if late < 15:
        return "apology_only", Decimal("0")
    if late <= 30:
        return "store_credit", Decimal("5.00")
    return "store_credit", min(money(Decimal(str(order["total"])) * Decimal("0.20")), Decimal("15.00"))


def _amount_check(proposal: dict[str, Any], order: dict[str, Any] | None, rules: set[str]) -> dict[str, Any]:
    resolution, amount = proposal.get("resolution_type"), _amount(proposal)
    if amount is None:
        return _result(False, "refund_amount is not a number")
    if resolution not in MONETARY_TYPES:
        return _result(amount == 0, "" if amount == 0 else f"{resolution} must have refund_amount 0")
    if order is None:
        return _result(None, "no order in the evidence")
    total = money(order["total"])
    if amount > total:
        return _result(False, f"refund {amount} exceeds the order total {total} (R11)")
    if resolution == "full_refund":
        return _result(amount == total, "" if amount == total else f"full_refund should equal the order total {total}")
    if resolution == "store_credit":
        if "R3" not in rules:
            return _result(False, "store credit is used only where a rule specifies it (R3, R11)")
        tier = _late_tier(order)
        if tier is None:
            return _result(None, "order has no delivery times")
        ok = tier == ("store_credit", amount)
        return _result(ok, "" if ok else f"lateness tier is {tier[0]} {tier[1]}, proposal has store_credit {amount}")
    lines = {item["name"]: item for item in order["items"]}
    affected = [lines[name] for name in proposal.get("affected_items") or [] if name in lines]
    if not affected:
        return _result(False, "partial_refund without affected items from the order")
    ceiling = sum(money(Decimal(str(i["unit_price"])) * i["quantity"]) for i in affected)
    if amount <= 0 or amount > ceiling:
        return _result(False, f"partial refund {amount} is outside 0 < amount <= {ceiling} for the affected items")
    if len(affected) == 1:
        unit = money(affected[0]["unit_price"])
        if (amount / unit) % 1 != 0:
            return _result(False, f"partial refund {amount} is not a whole number of {affected[0]['name']} ({unit})")
    return _result(True)


def run_checks(sample: dict[str, Any], policy: Policy) -> dict[str, dict[str, Any]]:
    """name -> {"passed": True|False|None (not applicable), "detail": str}."""
    proposal = sample.get("proposal")
    if not isinstance(proposal, dict):
        return {name: _result(None, "no proposal") for name in CHECKS}
    evidence = evidence_of(sample)
    order = evidence.orders.get(proposal.get("order_id"))
    rules = set(proposal.get("policy_rule_ids") or [])
    category = proposal.get("complaint_category")
    resolution = proposal.get("resolution_type")
    amount = _amount(proposal) or Decimal("0")
    monetary = resolution in MONETARY_TYPES and amount > 0
    complaint_date = (sample.get("intake") or {}).get("complaint_date") or ""
    results: dict[str, dict[str, Any]] = {}

    errors = schema_errors(proposal, load_schema_v2())
    results["schema_valid"] = _result(not errors, "; ".join(errors[:3]))

    if order is None:
        results["order_grounded"] = _result(False, "order_id was not returned by get_order")
    else:
        names = {item["name"] for item in order["items"]}
        unknown = [item for item in proposal.get("affected_items") or [] if item not in names]
        results["order_grounded"] = _result(not unknown, f"not on the order: {unknown}" if unknown else "")

    results["amount_grounded"] = _amount_check(proposal, order, rules)
    if (results["amount_grounded"]["passed"] is not False and order is not None and category == "late_delivery"
            and not rules & OVERRIDE_RULES):
        tier = _late_tier(order)
        if tier is not None and (resolution, amount) != tier:
            results["amount_grounded"] = _result(False, f"lateness tier is {tier[0]} {tier[1]}, proposal has "
                                                        f"{resolution} {amount}")

    unknown_rules = sorted(rules - set(policy.rule_ids))
    primary = {rule for rule in rules if policy.rule(rule).get("categories")} if not unknown_rules else set()
    fits = [rule for rule in primary if category in policy.rule(rule)["categories"]]
    if unknown_rules:
        results["rules_consistent"] = _result(False, f"unknown rule ids {unknown_rules}")
    elif rules & OVERRIDE_RULES:
        results["rules_consistent"] = _result(True)
    elif not fits:
        results["rules_consistent"] = _result(False, f"no cited rule covers category {category} (cited {sorted(rules)})")
    else:
        results["rules_consistent"] = _result(True)

    recent = recent_resolution_count(evidence.prior_resolutions or [], complaint_date) if complaint_date else 0
    required = resolution == "escalate_to_manager" or (monetary and (amount > Decimal(str(APPROVAL_THRESHOLD))
                                                                       or recent >= 2))
    flag = proposal.get("requires_human_approval")
    results["approval_flag"] = _result(flag is required, "" if flag is required else
                                       f"requires_human_approval is {flag}, policy gives {required} "
                                       f"(amount {amount}, {recent} recent resolutions)")

    compensated = _compensated(evidence, proposal.get("order_id"))
    results["prior_compensation"] = _result(not (compensated and monetary),
                                            "order already compensated (R13)" if compensated and monetary else "")

    if order is None or not complaint_date:
        results["filing_window"] = _result(None, "no order or complaint date")
    else:
        days = (parse_date(complaint_date) - parse_date(order["order_date"])).days
        late_filing = days > FILING_WINDOW_DAYS and category in DEFECT_CATEGORIES
        bad = late_filing and (monetary or resolution == "replacement")
        results["filing_window"] = _result(not bad, f"filed {days} days after the order (R12)" if bad else "")

    actions = sample.get("actions") or []
    monetary_actions = [a for a in actions if (a.get("amount") or 0) > 0]
    duplicate = len(actions) > 1 or (compensated and bool(monetary_actions))
    results["duplicate_actions"] = (_result(None, "no action recorded") if not actions and not compensated
                                    else _result(not duplicate, f"{len(actions)} recorded action(s)" if duplicate else ""))

    active = (sample.get("versions") or {}).get("policy_version")
    cited = proposal.get("policy_version")
    results["policy_version_current"] = _result(cited == active, "" if cited == active else
                                                f"cites policy {cited}, active policy is {active}")
    return results


def checks_passed(results: dict[str, dict[str, Any]]) -> bool | None:
    verdicts = [r["passed"] for r in results.values() if r["passed"] is not None]
    return None if not verdicts else all(verdicts)


@lru_cache(maxsize=1)
def _labels() -> dict[str, dict[str, Any]]:
    """case_id -> {"expected", "order"} for every labeled synthetic case (curated scenarios and generated rows)."""
    orders = {order["order_id"]: order for order in load_json(GENERATED_DIR / "orders.json")}
    labels: dict[str, dict[str, Any]] = {}
    with (GENERATED_DIR / "complaints.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("label_status") == "labeled" and row.get("case_id") and row.get("order_id") in orders:
                labels[row["case_id"]] = {"expected": row["expected"], "order": orders[row["order_id"]]}
    for scenario in load_json(AGENT_SCENARIOS_V2)["scenarios"]:
        order_id = scenario.get("expected_tools", {}).get("order_id")
        if order_id in orders and "resolution_type" in scenario.get("expected", {}):
            labels[scenario["intake"]["case_id"]] = {"expected": scenario["expected"], "order": orders[order_id]}
    return labels


def label_for(case_id: str) -> dict[str, Any] | None:
    return _labels().get(case_id)


def grade_label(sample: dict[str, Any]) -> dict[str, Any] | None:
    """grade() against the case's label, or None when the case has no label (production traffic)."""
    label = label_for(sample["case_id"])
    proposal = sample.get("proposal")
    if label is None or not isinstance(proposal, dict):
        return None
    return grade(proposal, label, schema_errors(proposal, load_schema_v2()))


def successful(sample: dict[str, Any], checks_ok: bool | None, label: dict[str, Any] | None) -> bool | None:
    """Planning decision 22: recorded (or a correct clarification) and passing the checks and any label.

    None while the case is still waiting for a reviewer.
    """
    status = sample.get("status")
    if status in {"pending_review", "investigating", "approved"}:
        return None
    if status == "needs_clarification":
        return sample.get("outcome_reason") in CORRECT_CLARIFICATION
    if not sample.get("actions"):
        return False
    return checks_ok is not False and (label is None or bool(label.get("passed")))

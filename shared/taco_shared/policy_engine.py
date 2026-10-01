"""Reference policy engine: deterministic expected outcomes from structured complaint facts.

Produces the `expected` block that `taco_shared.grading.grade` consumes. Used to label the dataset,
never by the agent or by `validate_proposal`.

Precedence: R7 (food safety) > R13 (already compensated) > R14 (unsupported) > R8 (cleanliness and
conduct) > R12 (filing window, order-defect rules only) > R1-R6. R9 and R10 add approval to
monetary outcomes; R11 caps amounts at the order total.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

MONETARY_TYPES = {"store_credit", "partial_refund", "full_refund"}
DEFECT_CATEGORIES = {"missing_items", "wrong_item", "late_delivery", "order_not_received", "wrong_address", "food_quality"}
APPROVAL_THRESHOLD = Decimal("25.00")
RECENT_DAYS = 90
FILING_WINDOW_DAYS = 14


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def as_number(value: Decimal) -> float | int:
    return 0 if value == 0 else float(value)


def parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def minutes_late(order: dict[str, Any]) -> float | None:
    promised, actual = order.get("promised_delivery_time"), order.get("actual_delivery_time")
    if not promised or not actual:
        return None
    return (datetime.fromisoformat(actual) - datetime.fromisoformat(promised)).total_seconds() / 60


def recent_resolution_count(prior_resolutions: list[dict[str, Any]], complaint_date: str) -> int:
    complaint = parse_date(complaint_date)
    return sum(1 for p in prior_resolutions if 0 <= (complaint - parse_date(p["date"])).days <= RECENT_DAYS)


def _late_outcome(order: dict[str, Any]) -> tuple[str, Decimal] | None:
    late = minutes_late(order)
    if late is None:
        return None
    if late < 15:
        return "apology_only", Decimal("0")
    if late <= 30:
        return "store_credit", Decimal("5.00")
    return "store_credit", min(money(Decimal(str(order["total"])) * Decimal("0.20")), Decimal("15.00"))


def _label(categories: list[str], options: dict[str, Decimal], affected: list[str], rules: list[str],
           approval: bool, alternatives: list[list[str]] | None = None, any_of: list[str] | None = None) -> dict[str, Any]:
    expected: dict[str, Any] = {
        "complaint_category": categories,
        "resolution_type": list(options),
        "refund_amount": as_number(next(iter(options.values()))) if len(options) == 1
        else {key: as_number(value) for key, value in options.items()},
        "affected_items": affected,
        "required_policy_rule_ids": rules,
        "requires_human_approval": approval,
    }
    if alternatives:
        expected["affected_items_alternatives"] = alternatives
    if any_of:
        expected["required_policy_rule_ids_any_of"] = any_of
    return expected


def evaluate(facts: dict[str, Any], order: dict[str, Any], prior_resolutions: list[dict[str, Any]],
             complaint_date: str) -> dict[str, Any]:
    """Return {"status": "labeled"|"ambiguous"|"multi_outcome", "expected": dict|None, "note": str}."""
    if facts.get("ambiguous"):
        return {"status": "ambiguous", "expected": None, "note": facts["ambiguous"]}

    category = facts["category"]
    affected = [item["name"] for item in facts.get("affected", [])]
    all_names = [item["name"] for item in order["items"]]
    total = money(order["total"])
    prices = {item["name"]: money(Decimal(str(item["unit_price"]))) for item in order["items"]}
    quantities = {item["name"]: item["quantity"] for item in order["items"]}

    if category == "food_safety":
        return _done(_label(["food_safety"], {"escalate_to_manager": Decimal("0")}, affected, ["R7"], True,
                            alternatives=[[]] if affected else None))

    if any(p.get("order_id") == order["order_id"] for p in prior_resolutions):
        return _done(_label([category], {"no_action": Decimal("0")}, [], ["R13"], False,
                            alternatives=[affected] if affected else None))

    if category == "other":
        return _done(_label(["other"], {"escalate_to_manager": Decimal("0")}, [], ["R14"], True))

    if category in {"cleanliness", "staff_conduct"}:
        return _done(_label([category], {"apology_only": Decimal("0")}, [], ["R8"], False))

    categories = [category]
    if category == "wrong_address" and facts.get("recovered"):
        categories = ["wrong_address", "late_delivery"]

    days = (parse_date(complaint_date) - parse_date(order["order_date"])).days
    if category in DEFECT_CATEGORIES and days > FILING_WINDOW_DAYS:
        return _done(_label(categories, {"apology_only": Decimal("0")}, affected, ["R12"], False,
                            alternatives=[[]] if affected else None))

    alternatives: list[list[str]] | None = None
    if category == "missing_items":
        if facts.get("named_item_not_on_order"):
            return _done(_label(categories, {"no_action": Decimal("0")}, [], ["R1"], False))
        missing = {item["name"]: item.get("quantity", 1) for item in facts["affected"]}
        if all(missing.get(name, 0) >= qty for name, qty in quantities.items()):
            categories, options, rules = ["missing_items", "order_not_received"], {"full_refund": total}, ["R4"]
            affected, alternatives = [], [all_names]
        else:
            amount = sum((prices[name] * qty for name, qty in missing.items()), Decimal("0"))
            options, rules = {"partial_refund": money(amount)}, ["R1"]
    elif category == "wrong_item":
        amount = sum((prices[name] * quantities[name] for name in affected), Decimal("0"))
        options, rules = {"replacement": Decimal("0"), "partial_refund": money(amount)}, ["R2"]
    elif category == "food_quality":
        amount = sum((prices[name] * quantities[name] for name in affected), Decimal("0"))
        options, rules = {"partial_refund": money(amount), "replacement": Decimal("0")}, ["R6"]
    elif category == "order_not_received":
        options, rules, affected, alternatives = {"full_refund": total}, ["R4"], [], [all_names]
    elif category == "wrong_address" and not facts.get("recovered"):
        options, rules, affected, alternatives = {"full_refund": total}, ["R5"], [], [all_names]
    elif category in {"late_delivery", "wrong_address"}:
        outcome = _late_outcome(order)
        if outcome is None:
            return {"status": "ambiguous", "expected": None, "note": "no delivery times on the order"}
        options, rules, affected = {outcome[0]: outcome[1]}, ["R3"], []
    else:
        return {"status": "ambiguous", "expected": None, "note": f"no rule for category {category}"}

    options = {key: min(value, total) for key, value in options.items()}
    recent = recent_resolution_count(prior_resolutions, complaint_date)
    decisions = set()
    extra_rules: set[str] = set()
    for resolution, amount in options.items():
        monetary = resolution in MONETARY_TYPES and amount > 0
        rule_additions = set()
        if monetary and amount > APPROVAL_THRESHOLD:
            rule_additions.add("R9")
        if monetary and recent >= 2:
            rule_additions.add("R10")
        decisions.add((bool(rule_additions), frozenset(rule_additions)))
        extra_rules |= rule_additions
    if len(decisions) > 1:
        return {"status": "multi_outcome", "expected": None,
                "note": "approval differs between permitted resolutions; grade() needs a single approval flag"}
    approval = next(iter(decisions))[0]
    rules = rules + sorted(extra_rules, key=lambda r: int(r[1:]))
    return _done(_label(categories, options, affected, rules, approval, alternatives=alternatives))


def _done(expected: dict[str, Any]) -> dict[str, Any]:
    return {"status": "labeled", "expected": expected, "note": ""}

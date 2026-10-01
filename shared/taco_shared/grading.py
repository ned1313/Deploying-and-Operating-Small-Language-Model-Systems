"""Deterministic grading of a resolution proposal against a labeled scenario."""

from __future__ import annotations

from typing import Any

AMOUNT_TOLERANCE = 0.011


def amount_matches(actual: Any, expected: Any, resolution_type: Any) -> bool:
    if not isinstance(actual, (int, float)) or isinstance(actual, bool):
        return False
    if isinstance(expected, dict):
        if resolution_type not in expected:
            return False
        expected = expected[resolution_type]
    return abs(float(actual) - float(expected)) <= AMOUNT_TOLERANCE


def grade(prediction: Any, scenario: dict[str, Any], schema_errors: list[str]) -> dict[str, Any]:
    expected = scenario["expected"]
    order = scenario["order"]
    order_items = {item["name"] for item in order["items"]}
    result: dict[str, Any] = {"schema_valid": not schema_errors, "issues": list(schema_errors)}
    if not isinstance(prediction, dict):
        result.update(grounded=False, policy_compliant=False, category_correct=False, passed=False)
        result["issues"].append("Output is not a JSON object.")
        return result

    resolution = prediction.get("resolution_type")
    refund = prediction.get("refund_amount")
    affected = prediction.get("affected_items") if isinstance(prediction.get("affected_items"), list) else []
    rules = prediction.get("policy_rule_ids") if isinstance(prediction.get("policy_rule_ids"), list) else []

    # Grounding: everything the model asserts must trace back to the known facts.
    order_id_ok = prediction.get("order_id") == order["order_id"]
    items_on_order = all(isinstance(item, str) and item in order_items for item in affected)
    refund_within_total = isinstance(refund, (int, float)) and not isinstance(refund, bool) and 0 <= refund <= order["total"] + AMOUNT_TOLERANCE
    accepted_item_sets = [expected["affected_items"], *expected.get("affected_items_alternatives", [])]
    affected_ok = any(sorted(affected) == sorted(option) for option in accepted_item_sets)
    grounded = order_id_ok and items_on_order and refund_within_total and affected_ok

    # Policy compliance: right resolution, right amount, right rules, right approval flag.
    resolution_ok = resolution in expected["resolution_type"]
    refund_ok = amount_matches(refund, expected["refund_amount"], resolution)
    required = set(expected.get("required_policy_rule_ids", []))
    any_of = set(expected.get("required_policy_rule_ids_any_of", []))
    rules_ok = required.issubset(set(rules)) and (not any_of or bool(any_of & set(rules)))
    approval_ok = prediction.get("requires_human_approval") is expected["requires_human_approval"]
    policy_compliant = resolution_ok and refund_ok and rules_ok and approval_ok

    category_ok = prediction.get("complaint_category") in expected["complaint_category"]

    for ok, message in (
        (order_id_ok, f"order_id should be {order['order_id']}"),
        (items_on_order, "affected_items contains names not on the order record"),
        (refund_within_total, f"refund_amount must be a number between 0 and the order total {order['total']}"),
        (affected_ok, f"affected_items should be one of {accepted_item_sets}"),
        (resolution_ok, f"resolution_type should be one of {expected['resolution_type']}, got {resolution!r}"),
        (refund_ok, f"refund_amount should be {expected['refund_amount']}, got {refund!r}"),
        (rules_ok, f"policy_rule_ids must include {sorted(required) or sorted(any_of)}, got {rules}"),
        (approval_ok, f"requires_human_approval should be {expected['requires_human_approval']}"),
        (category_ok, f"complaint_category should be one of {expected['complaint_category']}"),
    ):
        if not ok:
            result["issues"].append(message)

    result.update(
        order_id_ok=order_id_ok, items_on_order=items_on_order, refund_within_total=refund_within_total,
        affected_items_ok=affected_ok, grounded=grounded, resolution_ok=resolution_ok, refund_ok=refund_ok,
        rules_ok=rules_ok, approval_ok=approval_ok, policy_compliant=policy_compliant, category_correct=category_ok,
        passed=result["schema_valid"] and grounded and policy_compliant and category_ok,
    )
    return result

"""Pins grade() behaviour on the original module 1 scenarios (frozen copy of m1-screening-v1)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from taco_shared.grading import grade
from taco_shared.paths import load_json

from .conftest import FIXTURES

FROZEN = load_json(FIXTURES / "m1_screening_v1.json")["scenarios"]


def ideal_prediction(scenario: dict[str, Any]) -> dict[str, Any]:
    expected = scenario["expected"]
    resolution = expected["resolution_type"][0]
    amount = expected["refund_amount"]
    rules = expected.get("required_policy_rule_ids") or expected["required_policy_rule_ids_any_of"][:1]
    return {
        "order_id": scenario["order"]["order_id"], "complaint_category": expected["complaint_category"][0],
        "resolution_type": resolution, "refund_amount": amount[resolution] if isinstance(amount, dict) else amount,
        "affected_items": list(expected["affected_items"]), "policy_rule_ids": list(rules),
        "requires_human_approval": expected["requires_human_approval"],
        "customer_message": "ok", "justification": "ok",
    }


@pytest.mark.parametrize("scenario", FROZEN, ids=[s["scenario_id"] for s in FROZEN])
def test_ideal_prediction_passes(scenario: dict[str, Any]) -> None:
    result = grade(ideal_prediction(scenario), scenario, [])
    assert result["passed"], result["issues"]


@pytest.mark.parametrize("scenario", FROZEN, ids=[s["scenario_id"] for s in FROZEN])
def test_wrong_order_id_is_not_grounded(scenario: dict[str, Any]) -> None:
    prediction = ideal_prediction(scenario)
    prediction["order_id"] = "TA-00000"
    result = grade(prediction, scenario, [])
    assert not result["grounded"] and not result["passed"]


def test_amount_tolerance_and_alternatives() -> None:
    scenario = next(s for s in FROZEN if s["scenario_id"] == "S05")
    prediction = ideal_prediction(scenario)
    prediction.update(resolution_type="partial_refund", refund_amount=11.505)
    assert grade(prediction, scenario, [])["policy_compliant"]
    prediction["refund_amount"] = 11.6
    assert not grade(prediction, scenario, [])["policy_compliant"]


def test_schema_errors_fail_and_non_object() -> None:
    scenario = FROZEN[0]
    assert not grade(ideal_prediction(scenario), scenario, ["bad"])["passed"]
    assert grade(None, scenario, [])["passed"] is False


def test_approval_flag_must_match_exactly() -> None:
    scenario = next(s for s in FROZEN if s["scenario_id"] == "S02")
    prediction = copy.deepcopy(ideal_prediction(scenario))
    prediction["requires_human_approval"] = False
    assert not grade(prediction, scenario, [])["policy_compliant"]

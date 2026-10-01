"""Reference policy engine: rule-by-rule cases and parity with the reviewed module 1 labels."""

from __future__ import annotations

from typing import Any

import pytest

from taco_shared.paths import load_json
from taco_shared.policy_engine import evaluate

from .conftest import FIXTURES

FROZEN = {s["scenario_id"]: s for s in load_json(FIXTURES / "m1_screening_v1.json")["scenarios"]}


def item(name: str, quantity: int = 1) -> dict[str, Any]:
    return {"name": name, "quantity": quantity}


# Hand-written facts for the original module 1 scenarios (S15 is excluded: its reviewed label accepts two
# categories, which the engine deliberately does not produce).
FROZEN_FACTS = {
    "S01": {"category": "missing_items", "affected": [item("Chips and Guacamole")]},
    "S02": {"category": "missing_items", "affected": [item("Taco Family Pack")]},
    "S03": {"category": "missing_items", "affected": [item("Churros")]},
    "S04": {"category": "missing_items", "affected": [], "named_item_not_on_order": True},
    "S05": {"category": "wrong_item", "affected": [item("Steak Burrito")]},
    "S06": {"category": "late_delivery", "affected": []},
    "S07": {"category": "late_delivery", "affected": []},
    "S08": {"category": "late_delivery", "affected": []},
    "S09": {"category": "late_delivery", "affected": []},
    "S10": {"category": "order_not_received", "affected": []},
    "S11": {"category": "order_not_received", "affected": []},
    "S12": {"category": "wrong_address", "affected": [], "recovered": False},
    "S13": {"category": "wrong_address", "affected": [], "recovered": True},
    "S14": {"category": "food_quality", "affected": [item("Nachos Grande")]},
    "S16": {"category": "food_safety", "affected": [item("Chicken Burrito")]},
    "S17": {"category": "food_safety", "affected": [item("Taco Salad")]},
    "S18": {"category": "cleanliness", "affected": []},
    "S19": {"category": "staff_conduct", "affected": []},
    "S20": {"category": "missing_items", "affected": [item("Chips and Guacamole")]},
}


@pytest.mark.parametrize("scenario_id", sorted(FROZEN_FACTS))
def test_engine_matches_reviewed_m1_labels(scenario_id: str) -> None:
    scenario = FROZEN[scenario_id]
    label = evaluate(FROZEN_FACTS[scenario_id], scenario["order"], scenario["prior_resolutions"],
                     scenario["complaint_date"])
    assert label["status"] == "labeled", label["note"]
    expected = label["expected"]
    for key, value in scenario["expected"].items():
        assert expected[key] == value, f"{scenario_id} {key}: engine {expected[key]!r} != reviewed {value!r}"


def order(total: float = 30.0, order_date: str = "2026-03-01", late: int | None = None,
          items: list[tuple[str, int, float]] | None = None) -> dict[str, Any]:
    lines = items or [("Chicken Taco", 2, 3.95), ("Churros", 1, 4.50)]
    result = {"order_id": "TA-10001", "order_date": order_date, "total": total,
              "items": [{"name": n, "quantity": q, "unit_price": p} for n, q, p in lines],
              "promised_delivery_time": None, "actual_delivery_time": None}
    if late is not None:
        result["promised_delivery_time"] = f"{order_date}T18:00:00"
        hour, minute = divmod(18 * 60 + late, 60)
        result["actual_delivery_time"] = f"{order_date}T{hour:02d}:{minute:02d}:00"
    return result


def run(facts: dict[str, Any], o: dict[str, Any], priors: list[dict[str, Any]] | None = None,
        complaint_date: str = "2026-03-01") -> dict[str, Any]:
    label = evaluate(facts, o, priors or [], complaint_date)
    assert label["status"] == "labeled", label
    return label["expected"]


def test_r3_boundaries() -> None:
    late = {"category": "late_delivery", "affected": []}
    assert run(late, order(late=14))["resolution_type"] == ["apology_only"]
    assert run(late, order(late=15))["refund_amount"] == 5.0
    assert run(late, order(late=30))["refund_amount"] == 5.0
    assert run(late, order(total=41.79, late=31))["refund_amount"] == 8.36
    assert run(late, order(total=120.0, late=45))["refund_amount"] == 15.0


def test_r1_all_items_missing_becomes_r4() -> None:
    facts = {"category": "missing_items", "affected": [item("Chicken Taco", 2), item("Churros", 1)]}
    expected = run(facts, order(total=14.0))
    assert expected["resolution_type"] == ["full_refund"] and expected["required_policy_rule_ids"] == ["R4"]


def test_r9_and_r10_add_approval() -> None:
    facts = {"category": "missing_items", "affected": [item("Taco Family Pack")]}
    big = order(total=60.0, items=[("Taco Family Pack", 1, 32.0), ("Churros", 1, 4.5)])
    assert run(facts, big)["required_policy_rule_ids"] == ["R1", "R9"]
    priors = [{"date": "2026-02-01", "order_id": "TA-90001"}, {"date": "2026-01-15", "order_id": "TA-90002"}]
    expected = run({"category": "missing_items", "affected": [item("Churros")]}, order(), priors)
    assert expected["required_policy_rule_ids"] == ["R1", "R10"] and expected["requires_human_approval"]


def test_r10_ignores_old_resolutions() -> None:
    priors = [{"date": "2025-10-01", "order_id": "TA-90001"}, {"date": "2026-02-01", "order_id": "TA-90002"}]
    expected = run({"category": "missing_items", "affected": [item("Churros")]}, order(), priors)
    assert expected["requires_human_approval"] is False


def test_r12_r13_r14_precedence() -> None:
    old = order(order_date="2026-02-01")
    assert run({"category": "missing_items", "affected": [item("Churros")]}, old)["required_policy_rule_ids"] == ["R12"]
    assert run({"category": "food_safety", "affected": []}, old)["required_policy_rule_ids"] == ["R7"]
    assert run({"category": "other", "affected": []}, old)["required_policy_rule_ids"] == ["R14"]
    same = [{"date": "2026-03-01", "order_id": "TA-10001"}]
    assert run({"category": "missing_items", "affected": [item("Churros")]}, order(), same)["resolution_type"] == ["no_action"]
    assert run({"category": "food_safety", "affected": []}, order(), same)["resolution_type"] == ["escalate_to_manager"]


def test_ambiguous_and_multi_outcome() -> None:
    assert evaluate({"category": "food_quality", "affected": [], "ambiguous": "no item"}, order(), [],
                    "2026-03-01")["status"] == "ambiguous"
    priors = [{"date": "2026-02-01", "order_id": "TA-90001"}, {"date": "2026-02-10", "order_id": "TA-90002"}]
    label = evaluate({"category": "wrong_item", "affected": [item("Churros")]}, order(), priors, "2026-03-01")
    assert label["status"] == "multi_outcome"

"""Dataset build: reproducibility, cleaning, curated labels, and consistency between renderings."""

from __future__ import annotations

import json

import pytest

from taco_shared.dataset import build as build_module
from taco_shared.paths import CURATED_SCENARIOS, GENERATED_DIR, load_json


@pytest.fixture(scope="module")
def built() -> dict:
    return build_module.build()


def test_committed_generated_files_match_a_rebuild(built: dict) -> None:
    for name, content in built.items():
        committed = (GENERATED_DIR / name).read_text(encoding="utf-8")
        assert committed == build_module.serialize(name, content), f"{name} is stale; run python -m taco_shared.dataset.build"


def test_defective_rows_are_dropped_and_reported(built: dict) -> None:
    report = built["build_report.json"]
    reasons = " ".join(row["reason"] for row in report["dropped"])
    assert report["rows_read"] == 1000 and report["rows_dropped"] == len(report["dropped"]) == 20
    assert "impossible date" in reasons and "future date" in reasons and "empty message" in reasons
    assert "malformed customer_id" in reasons


def test_order_ids_are_unique_and_in_complaint_text(built: dict) -> None:
    complaints = built["complaints.jsonl"]
    ids = [c["order_id"] for c in complaints]
    assert len(ids) == len(set(ids))
    for complaint in complaints:
        assert complaint["order_id"] == f"TA-{10001 + complaint['source_row']}"
        assert complaint["order_id"] in complaint["complaint"]


def test_every_row_has_a_label_status(built: dict) -> None:
    statuses = {c["label_status"] for c in built["complaints.jsonl"]}
    assert statuses <= {"labeled", "ambiguous", "multi_outcome"}
    assert all(c["expected"] for c in built["complaints.jsonl"] if c["label_status"] == "labeled")


def test_curated_scenarios(built: dict) -> None:
    curated = load_json(CURATED_SCENARIOS)["scenarios"]
    agent = {s["scenario_id"]: s for s in built["agent_scenarios.json"]["scenarios"]}
    screening = {s["scenario_id"]: s for s in built["screening_scenarios.json"]["scenarios"]}
    assert len(agent) == 24 and len(screening) == 22 and {"A01", "A02"}.isdisjoint(screening)
    assert set(agent) == {entry["id"] for entry in curated}
    a01 = agent["A01"]["expected"]
    assert a01["affected_items"] == ["Carnitas Taco"] and a01["refund_amount"] == 8.5
    assert agent["A02"]["expected"]["required_policy_rule_ids"] == ["R13"]
    assert agent["S22"]["expected"]["required_policy_rule_ids"] == ["R13"]
    assert agent["S21"]["expected"]["required_policy_rule_ids"] == ["R14"]
    for scenario in agent.values():
        assert "order_id" not in scenario["intake"]
        assert scenario["expected_tools"]["order_id"] in scenario["intake"]["complaint"]


def test_screening_order_matches_database_order(built: dict, app_db) -> None:
    for scenario in built["screening_scenarios.json"]["scenarios"]:
        inlined = scenario["order"]
        stored = app_db.get_order(inlined["order_id"], scenario["customer_id"], (scenario["store_id"],))
        assert stored is not None
        assert stored["total"] == inlined["total"]
        assert [(i["name"], i["quantity"], i["unit_price"]) for i in stored["items"]] == \
               [(i["name"], i["quantity"], i["unit_price"]) for i in inlined["items"]]


def test_serialization_is_deterministic(built: dict) -> None:
    again = build_module.build()
    assert json.dumps(built, sort_keys=True, default=str) == json.dumps(again, sort_keys=True, default=str)

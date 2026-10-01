"""Proposal schemas, policy rendering, and the evidence-based validator."""

from __future__ import annotations

import copy
import json
from typing import Any

from taco_shared.paths import PROPOSAL_SCHEMA_V2, load_json, policy_text_path
from taco_shared.policy import load_policy
from taco_shared.proposal import Evidence, ProposalV2, build_v2_schema, load_schema_v1, validate_proposal

from .conftest import FIXTURES


def test_v1_differs_from_m1_schema_only_by_new_rule_ids() -> None:
    original = load_json(FIXTURES / "m1_proposal_schema_v1.json")
    current = load_schema_v1()
    enum = current["properties"]["policy_rule_ids"]["items"]["enum"]
    assert enum[-2:] == ["R13", "R14"]
    current["properties"]["policy_rule_ids"]["items"]["enum"] = enum[:-2]
    assert current == original


def test_v2_is_generated_and_a_superset_of_v1() -> None:
    v1, v2 = load_schema_v1(), load_json(PROPOSAL_SCHEMA_V2)
    assert v2 == build_v2_schema(v1)
    for key, value in v1["properties"].items():
        assert v2["properties"][key] == value
    assert set(v1["required"]) < set(v2["required"]) and set(v2["required"]) == set(v2["properties"])


def test_policy_text_is_rendered_from_json() -> None:
    policy = load_policy()
    assert policy_text_path().read_text(encoding="utf-8") == policy.render_text()
    assert policy.rule_ids == [f"R{i}" for i in range(1, 15)]
    ids = [rule["id"] for rule in policy.rules_for_category("late_delivery")]
    assert ids == ["R3", "R7", "R9", "R10", "R11", "R12", "R13", "R14"]


ORDER = {"order_id": "TA-10431", "order_date": "2026-04-13", "total": 27.84,
         "items": [{"name": "Carnitas Taco", "quantity": 3, "unit_price": 4.25}, {"name": "Horchata", "quantity": 1, "unit_price": 3.25}]}


def evidence(priors: list[dict[str, Any]] | None = None, case: dict[str, Any] | None = None) -> Evidence:
    ev = Evidence()
    ev.add("get_order", {"status": "ok", "record_id": "order:TA-10431", "order": ORDER})
    ev.add("get_prior_resolutions", {"status": "ok", "record_id": "resolutions:x", "resolutions": priors or []})
    ev.add("get_resolution_policy", json.dumps({"status": "ok", "record_id": "policy:2026.09", "policy_version": "2026.09",
                                                "rules": [{"id": "R1", "record_id": "policy:2026.09#R1"}]}))
    if case:
        ev.add("get_case_status", {"status": "ok", "record_id": "case:CASE-10431", "case": case})
    return ev


def proposal(**overrides: Any) -> dict[str, Any]:
    base = {"case_id": "CASE-10431", "order_id": "TA-10431", "complaint_category": "missing_items",
            "resolution_type": "partial_refund", "refund_amount": 8.5, "affected_items": ["Carnitas Taco"],
            "policy_rule_ids": ["R1"], "requires_human_approval": False, "customer_message": "Sorry.",
            "justification": "Two tacos missing.", "policy_version": "2026.09",
            "evidence_refs": [{"tool": "get_order", "record_id": "order:TA-10431"},
                              {"tool": "get_resolution_policy", "record_id": "policy:2026.09#R1"}],
            "clarification_or_escalation_reason": ""}
    base.update(overrides)
    return base


def violations(p: dict[str, Any], ev: Evidence | None = None, date: str = "2026-04-13") -> list[str]:
    report = validate_proposal(p, ev or evidence(), date)
    assert not report.schema_errors, report.schema_errors
    return report.business_rule_violations


def test_valid_proposal_is_accepted() -> None:
    report = validate_proposal(proposal(), evidence(), "2026-04-13")
    assert report.accepted, report.to_dict()
    ProposalV2.model_validate(proposal())


def test_schema_errors_are_reported() -> None:
    bad = proposal()
    del bad["evidence_refs"]
    assert validate_proposal(bad, evidence(), "2026-04-13").schema_errors


def test_each_business_rule() -> None:
    assert any("not returned by get_order" in v for v in violations(proposal(order_id="TA-99999")))
    assert any("not on the order" in v for v in violations(proposal(affected_items=["Nachos Grande"])))
    assert any("exceeds the order total" in v for v in violations(proposal(refund_amount=30.0)))
    assert any("must have refund_amount 0" in v for v in violations(proposal(resolution_type="apology_only")))
    assert any("policy_version" in v for v in violations(proposal(policy_version="2025.01")))
    assert any("evidence_refs" in v for v in violations(proposal(evidence_refs=[{"tool": "get_order", "record_id": "order:TA-1"}])))
    assert any("R7/R14" in v for v in violations(proposal(complaint_category="food_safety", refund_amount=0,
                                                         resolution_type="apology_only")))
    assert any("R9" in v for v in violations(proposal(refund_amount=26.0)))
    recent = [{"date": "2026-03-01", "order_id": "TA-90001", "amount": 5.0}, {"date": "2026-04-01", "order_id": "TA-90002", "amount": 4.0}]
    assert any("R10" in v for v in violations(proposal(), evidence(priors=recent)))
    assert any("R12" in v for v in violations(proposal(), date="2026-05-10"))
    same = [{"date": "2026-04-13", "order_id": "TA-10431", "amount": 8.5}]
    assert any("R13" in v for v in violations(proposal(), evidence(priors=same)))
    case = {"order_id": "TA-10431", "recorded_actions": [{"action_id": "A", "amount": 8.5}]}
    assert any("R13" in v for v in violations(proposal(), evidence(case=case)))


def test_missing_policy_lookup_is_a_violation() -> None:
    ev = copy.deepcopy(evidence())
    ev.policy_version = None
    assert any("not retrieved" in v for v in violations(proposal(), ev))

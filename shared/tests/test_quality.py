"""Deterministic quality checks, labels, sampling decisions, and the evaluation store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taco_shared.agent.tools import build_investigation_tools
from taco_shared.context import build_context
from taco_shared.policy import load_policy
from taco_shared.quality.checks import CHECKS, checks_passed, grade_label, label_for, run_checks, successful
from taco_shared.quality.sampling import in_sample, sample_reason
from taco_shared.quality.store import QualityStore


def sample_for(case_id: str, app_db, case_client, **proposal_changes) -> dict:
    """A sample whose proposal is built from the case's label, with real tool results as evidence."""
    label = label_for(case_id)
    expected, order = label["expected"], label["order"]
    rows = {json.loads(line)["case_id"]: json.loads(line) for line in
            (Path(__file__).resolve().parents[1] / "taco_shared/data/generated/complaints.jsonl").open(encoding="utf-8")}
    row = rows[case_id]
    ctx = build_context("rep-multi-01", row["customer_id"], case_id, row["complaint_date"])
    tools = {t.name: t for t in build_investigation_tools(ctx, app_db, case_client, load_policy())}
    evidence = [{"tool": name, "result": json.loads(tools[name].invoke(args))} for name, args in (
        ("get_order", {"order_id": order["order_id"]}), ("get_prior_resolutions", {"lookback_days": 90}),
        ("get_resolution_policy", {"complaint_category": expected["complaint_category"][0]}), ("get_case_status", {}))]
    resolution = expected["resolution_type"][0]
    amount = expected["refund_amount"][resolution] if isinstance(expected["refund_amount"], dict) else expected["refund_amount"]
    proposal = {"case_id": case_id, "order_id": order["order_id"], "complaint_category": expected["complaint_category"][0],
                "resolution_type": resolution, "refund_amount": amount, "affected_items": expected["affected_items"],
                "policy_rule_ids": expected["required_policy_rule_ids"] or expected.get("required_policy_rule_ids_any_of", [])[:1],
                "requires_human_approval": expected["requires_human_approval"], "customer_message": "Sorry.",
                "justification": "Per policy.", "policy_version": "2026.09",
                "evidence_refs": [{"tool": "get_order", "record_id": f"order:{order['order_id']}"}],
                "clarification_or_escalation_reason": "", **proposal_changes}
    return {"case_id": case_id, "sample_id": f"{case_id}:t", "status": "recorded", "proposal": proposal,
            "evidence": evidence, "intake": {"complaint_date": row["complaint_date"]},
            "versions": {"policy_version": "2026.09", "model": "m"}, "actions": [{"action_id": "APR-1", "amount": amount}]}


def scenario_case(scenario_id: str) -> str:
    from taco_shared.paths import AGENT_SCENARIOS_V2, load_json
    return next(s["intake"]["case_id"] for s in load_json(AGENT_SCENARIOS_V2)["scenarios"] if s["scenario_id"] == scenario_id)


def test_correct_proposal_passes_every_check(app_db, case_client) -> None:
    sample = sample_for(scenario_case("A01"), app_db, case_client)
    results = run_checks(sample, load_policy())
    assert set(results) == set(CHECKS)
    assert checks_passed(results) is True, {k: v for k, v in results.items() if v["passed"] is False}
    label = grade_label(sample)
    assert label["passed"] and successful(sample, True, label) is True


def test_late_delivery_credit_is_caught_by_checks(app_db, case_client) -> None:
    """S09 as Qwen answered it: schema-valid and accepted by the validator, but the wrong lateness tier."""
    sample = sample_for(scenario_case("S09"), app_db, case_client, resolution_type="store_credit", refund_amount=5.0)
    results = run_checks(sample, load_policy())
    assert results["amount_grounded"]["passed"] is False and "lateness tier" in results["amount_grounded"]["detail"]
    assert results["schema_valid"]["passed"] is True
    assert successful(sample, checks_passed(results), grade_label(sample)) is False


def test_unneeded_approval_flag_is_caught(app_db, case_client) -> None:
    sample = sample_for(scenario_case("S12"), app_db, case_client, requires_human_approval=True)
    assert run_checks(sample, load_policy())["approval_flag"]["passed"] is False


def test_misclassification_needs_the_label(app_db, case_client) -> None:
    """S15 as Qwen answered it: a consistent wrong_item/R2 proposal. Only the label catches it."""
    sample = sample_for(scenario_case("S15"), app_db, case_client, complaint_category="wrong_item", policy_rule_ids=["R2"],
                        resolution_type="partial_refund", refund_amount=4.5)
    assert checks_passed(run_checks(sample, load_policy())) is True
    assert grade_label(sample)["passed"] is False


def test_outcomes_without_a_proposal() -> None:
    assert successful({"status": "needs_clarification", "outcome_reason": "missing_order_id"}, None, None) is True
    assert successful({"status": "escalated", "outcome_reason": "dependency_unavailable", "actions": []}, None, None) is False
    assert successful({"status": "pending_review"}, True, None) is None
    assert run_checks({"proposal": None}, load_policy())["schema_valid"]["passed"] is None


def test_sampling_is_deterministic_and_always_samples_escalations() -> None:
    picked = [case for case in (f"CASE-{n}" for n in range(1000)) if in_sample(case, 0.25)]
    assert 180 < len(picked) < 320 and picked == [c for c in (f"CASE-{n}" for n in range(1000)) if in_sample(c, 0.25)]
    assert sample_reason("CASE-1", "escalated", 0, 0.01) == "always:escalated"
    assert sample_reason("CASE-1", "pending_review", 1, 0.01) == "always:corrected"
    assert sample_reason("CASE-1", "escalated", 0, 0) is None
    assert sample_reason("CASE-1", "investigating", 0, 1.0) is None


def test_store_scores_queue_and_reviews(tmp_path: Path) -> None:
    store = QualityStore(tmp_path / "quality.db")
    record = {"sample_id": "T1", "case_id": "CASE-1", "status": "recorded", "versions": {"model": "m"}, "actions": []}
    store.upsert_sample(record, "rate")
    pending = list(store.samples_to_score())
    assert [s["sample_id"] for s in pending] == ["T1"]
    store.save_score(pending[0], {"x": {"passed": False, "detail": "d"}}, False, None, False, ["x: d"])
    assert list(store.samples_to_score()) == [] and store.queue()[0]["flag_reasons"] == ["x: d"]
    with pytest.raises(ValueError):
        store.add_review("T1", "qa-01", 9, 3, 3, True, "ok", None)
    store.add_review("T1", "qa-01", 4, 3, 3, True, "ok", "fine")
    assert store.queue() == [] and store.counts()["reviews"] == 1
    assert store.recent()[0]["successful"] == 0
    reader = QualityStore(tmp_path / "quality.db", read_only=True)
    assert reader.counts()["samples"] == {"recorded": 1}

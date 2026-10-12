"""The module 3 workflow graph with a scripted model, a real SQLite checkpointer, and the in-process case API."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from taco_shared.agent.faults import FaultPlan, InferenceUnavailable, ModelFault
from taco_shared.case_api_client import CaseApiClient
from taco_shared.policy import load_policy
from taco_shared.testing import ScriptedChatModel, InProcessTransport, load_case_api_module, tool_call
from taco_shared.workflow import WorkflowDeps, WorkflowSettings, build_graph

A01_CASE, A01_ORDER, A01_CUSTOMER = "CASE-10431", "TA-10431", "CUST-14159"
A01_COMPLAINT = ("Two of the three carnitas tacos I ordered were missing from my delivery bag. Everything else was "
                 "there. Order TA-10431.")
PROPOSAL = {
    "case_id": A01_CASE, "order_id": A01_ORDER, "complaint_category": "missing_items",
    "resolution_type": "partial_refund", "refund_amount": 8.5, "affected_items": ["Carnitas Taco"],
    "policy_rule_ids": ["R1"], "requires_human_approval": False, "customer_message": "Sorry about the tacos.",
    "justification": "Two of three Carnitas Tacos missing; R1.", "policy_version": "2026.09",
    "evidence_refs": [{"tool": "get_order", "record_id": f"order:{A01_ORDER}"},
                      {"tool": "get_resolution_policy", "record_id": "policy:2026.09#R1"}],
    "clarification_or_escalation_reason": "",
}


def investigation(*, skip_policy: bool = False) -> list[AIMessage]:
    turns = [AIMessage(content="", tool_calls=[tool_call("get_order", {"order_id": A01_ORDER}, "c1")]),
             AIMessage(content="", tool_calls=[tool_call("get_prior_resolutions", {"lookback_days": 90}, "c2")])]
    if not skip_policy:
        turns.append(AIMessage(content="", tool_calls=[
            tool_call("get_resolution_policy", {"complaint_category": "missing_items"}, "c3")]))
    turns += [AIMessage(content="", tool_calls=[tool_call("get_case_status", {}, "c4")]),
              AIMessage(content="Two Carnitas Tacos are missing; R1 applies.")]
    return turns


@pytest.fixture
def case_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TACO_ADMIN_TOKEN", "test-admin")
    module = load_case_api_module(tmp_path / "cases.db")
    with TestClient(module.app) as client:
        yield client


@pytest.fixture
def cases(case_api: TestClient) -> CaseApiClient:
    return CaseApiClient("http://testserver", transport=InProcessTransport(case_api), retries=2, sleep=lambda _: None)


class Harness:
    def __init__(self, tmp_path: Path, app_db, cases: CaseApiClient, model: ScriptedChatModel,
                 settings: WorkflowSettings | None = None) -> None:
        self.path = tmp_path / "checkpoints.db"
        self.deps = WorkflowDeps(model=model, db=app_db, cases=cases, policy=load_policy(),
                                 settings=settings or WorkflowSettings())
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.deps.observer = lambda event, data: self.events.append((event, data))
        self.restart()

    def restart(self) -> None:
        """A new compiled graph and checkpointer connection, as after a container restart."""
        self.saver = SqliteSaver(sqlite3.connect(self.path, check_same_thread=False))
        self.app = build_graph(self.deps).compile(checkpointer=self.saver)

    def config(self, thread: str = "t1") -> dict[str, Any]:
        return {"configurable": {"thread_id": thread}, "recursion_limit": 100}

    def start(self, complaint: str = A01_COMPLAINT, case_id: str = A01_CASE, actor: str = "rep-kop-01",
              stores: tuple[str, ...] = ("KOP",), thread: str = "t1") -> dict[str, Any]:
        intake = {"case_id": case_id, "customer_id": A01_CUSTOMER, "complaint_date": "2026-04-13",
                  "complaint_channel": "Google Review", "complaint": complaint}
        return self.app.invoke({"request_id": "r1", "actor_id": actor, "store_ids": list(stores), "intake": intake,
                                "tool_calls_used": 0, "repeated_calls": 0, "nudges_used": 0, "corrections_used": 0,
                                "proposal_version": 0, "status": "new"}, self.config(thread))

    def state(self, thread: str = "t1"):
        return self.app.get_state(self.config(thread))

    def resume(self, decision: dict[str, Any], thread: str = "t1") -> dict[str, Any]:
        return self.app.invoke(Command(resume=decision), self.config(thread))


def test_pause_restart_approve_record_is_idempotent(tmp_path, app_db, cases, case_api) -> None:
    h = Harness(tmp_path, app_db, cases, ScriptedChatModel(responses=investigation(), proposals=[dict(PROPOSAL)]))
    h.start()
    snapshot = h.state()
    assert snapshot.next == ("human_review",) and snapshot.interrupts
    assert snapshot.interrupts[0].value["proposal_version"] == 1
    h.restart()
    assert h.state().next == ("human_review",)
    final = h.resume({"decision": "approve", "reviewer": "sup-kop-01"})
    assert final["status"] == "recorded" and final["action"]["amount"] == 8.5 and not final["action_replayed"]
    actions = [a for a in cases.get_case(A01_CASE, ("KOP",))["recorded_actions"] if a["action_id"].startswith("APR-")]
    assert len(actions) == 1
    replay = cases.record_action(A01_CASE, ("KOP",), {"type": "partial_refund", "amount": 8.5, "proposal_version": 1,
                                                      "approved_by": "sup-kop-01"}, f"{A01_CASE}:v1")
    assert replay["replayed"] is True
    assert len(cases.get_case(A01_CASE, ("KOP",))["recorded_actions"]) == 1
    assert {e for e, _ in h.events} >= {"node", "tool", "model"}


def test_missing_order_id_clarifies_without_calling_the_model(tmp_path, app_db, cases) -> None:
    model = ScriptedChatModel(responses=[], proposals=[])
    h = Harness(tmp_path, app_db, cases, model)
    final = h.start(complaint="Two of my carnitas tacos were missing.")
    assert final["status"] == "needs_clarification" and final["outcome_reason"] == "missing_order_id"
    assert model.calls == []


def test_unknown_order_clarifies(tmp_path, app_db, cases) -> None:
    model = ScriptedChatModel(responses=[
        AIMessage(content="", tool_calls=[tool_call("get_order", {"order_id": "TA-99999"}, "c1")]),
        AIMessage(content="The order was not found.")], proposals=[])
    final = Harness(tmp_path, app_db, cases, model).start(complaint="Tacos missing. Order TA-99999.")
    assert final["status"] == "needs_clarification" and final["outcome_reason"] == "order_not_found"


def test_missing_policy_lookup_gets_one_nudge(tmp_path, app_db, cases) -> None:
    responses = investigation(skip_policy=True) + [
        AIMessage(content="", tool_calls=[tool_call("get_resolution_policy", {"complaint_category": "missing_items"}, "c5")]),
        AIMessage(content="Now I have the policy.")]
    model = ScriptedChatModel(responses=responses, proposals=[dict(PROPOSAL)])
    h = Harness(tmp_path, app_db, cases, model)
    h.start()
    values = h.state().values
    assert values["nudges_used"] == 1 and h.state().next == ("human_review",)
    assert any(s["kind"] == "nudge" for s in values["steps"])


def test_missing_evidence_escalates_after_the_nudge(tmp_path, app_db, cases) -> None:
    model = ScriptedChatModel(responses=[AIMessage(content="Refund it."), AIMessage(content="Still refund it.")],
                              proposals=[])
    final = Harness(tmp_path, app_db, cases, model).start()
    assert final["status"] == "escalated" and final["outcome_reason"].startswith("missing_evidence")


def test_repeated_call_is_not_executed(tmp_path, app_db, cases) -> None:
    responses = investigation()
    responses.insert(1, AIMessage(content="", tool_calls=[tool_call("get_order", {"order_id": A01_ORDER}, "dup")]))
    h = Harness(tmp_path, app_db, cases, ScriptedChatModel(responses=responses, proposals=[dict(PROPOSAL)]))
    h.start()
    values = h.state().values
    assert values["repeated_calls"] == 1
    duplicate = [e for e in values["evidence_log"] if not e["executed"]]
    assert len(duplicate) == 1 and "duplicate_call" in duplicate[0]["content"]


def test_tool_budget_ends_the_investigation(tmp_path, app_db, cases) -> None:
    looping = [AIMessage(content="", tool_calls=[tool_call("get_prior_resolutions", {"lookback_days": d}, f"c{d}")])
               for d in range(1, 10)]
    h = Harness(tmp_path, app_db, cases, ScriptedChatModel(responses=looping, proposals=[]),
                WorkflowSettings(max_tool_calls=3))
    final = h.start()
    assert final["tool_calls_used"] == 3 and final["status"] == "escalated"


def test_invalid_proposal_gets_one_correction(tmp_path, app_db, cases) -> None:
    bad = {**PROPOSAL, "affected_items": ["Lobster Taco"]}
    model = ScriptedChatModel(responses=investigation(), proposals=[bad, dict(PROPOSAL)])
    h = Harness(tmp_path, app_db, cases, model)
    h.start()
    values = h.state().values
    assert values["corrections_used"] == 1 and values["proposal_version"] == 2
    assert "Lobster Taco" in model.calls[-1][-2].content


def test_second_invalid_proposal_escalates(tmp_path, app_db, cases) -> None:
    bad = {**PROPOSAL, "affected_items": ["Lobster Taco"]}
    final = Harness(tmp_path, app_db, cases,
                    ScriptedChatModel(responses=investigation(), proposals=[bad, dict(bad)])).start()
    assert final["status"] == "escalated" and final["outcome_reason"] == "validation_failed"


def test_injected_malformed_json_is_corrected(tmp_path, app_db, cases) -> None:
    h = Harness(tmp_path, app_db, cases,
                ScriptedChatModel(responses=investigation(), proposals=[dict(PROPOSAL), dict(PROPOSAL)]))
    h.deps.faults.set([ModelFault(mode="malformed_json", node="propose", count=1)])
    h.start()
    steps = [s for s in h.state().values["steps"] if s["kind"] == "finalize"]
    assert steps[0]["injected_fault"] == "malformed_json" and steps[1]["injected_fault"] is None
    assert h.state().next == ("human_review",)


def test_case_api_outage_escalates_after_retries(tmp_path, app_db, cases, case_api) -> None:
    case_api.put("/admin/faults", json={"mode": "status", "status": 503}, headers={"X-Admin-Token": "test-admin"})
    final = Harness(tmp_path, app_db, cases,
                    ScriptedChatModel(responses=investigation(), proposals=[])).start()
    assert final["status"] == "escalated" and final["outcome_reason"] == "dependency_unavailable"
    assert [e["retries"] for e in final["evidence_log"] if e["tool"] == "get_case_status"] == [2]


def test_case_api_503_once_recovers(tmp_path, app_db, cases, case_api) -> None:
    case_api.put("/admin/faults", json={"mode": "status", "status": 503, "count": 1},
                 headers={"X-Admin-Token": "test-admin"})
    h = Harness(tmp_path, app_db, cases, ScriptedChatModel(responses=investigation(), proposals=[dict(PROPOSAL)]))
    h.start()
    assert h.state().next == ("human_review",)


def test_inference_outage_resumes_from_checkpoint(tmp_path, app_db, cases) -> None:
    h = Harness(tmp_path, app_db, cases, ScriptedChatModel(responses=investigation(), proposals=[dict(PROPOSAL)]))
    h.deps.faults.set([ModelFault(mode="inference_down")])
    with pytest.raises(InferenceUnavailable):
        h.start()
    h.deps.faults.clear()
    h.restart()
    h.app.invoke(None, h.config())
    assert h.state().next == ("human_review",)


def test_connection_error_becomes_inference_unavailable(tmp_path, app_db, cases) -> None:
    import httpx
    import openai

    error = openai.APIConnectionError(request=httpx.Request("POST", "http://gateway/v1/chat/completions"))
    h = Harness(tmp_path, app_db, cases, ScriptedChatModel(responses=[error], proposals=[]))
    with pytest.raises(InferenceUnavailable):
        h.start()
    assert h.state().next == ("investigate",)


def test_crash_mid_investigation_resumes_after_restart(tmp_path, app_db, cases) -> None:
    responses = investigation()
    responses.insert(2, RuntimeError("container killed"))
    model = ScriptedChatModel(responses=responses, proposals=[dict(PROPOSAL)])
    h = Harness(tmp_path, app_db, cases, model)
    with pytest.raises(RuntimeError):
        h.start()
    assert h.state().values["tool_calls_used"] == 2
    h.restart()
    h.app.invoke(None, h.config())
    values = h.state().values
    assert h.state().next == ("human_review",) and values["tool_calls_used"] == 4


def test_revision_creates_a_new_version_and_reject_ends(tmp_path, app_db, cases) -> None:
    model = ScriptedChatModel(responses=investigation(), proposals=[dict(PROPOSAL), dict(PROPOSAL)])
    h = Harness(tmp_path, app_db, cases, model)
    h.start()
    h.resume({"decision": "revise", "reviewer": "sup-kop-01", "note": "Mention the replacement option."})
    assert h.state().interrupts[0].value["proposal_version"] == 2
    assert "Mention the replacement option." in model.calls[-1][-2].content
    final = h.resume({"decision": "reject", "reviewer": "sup-kop-01"})
    assert final["status"] == "rejected" and [r["decision"] for r in final["reviews"]] == ["revise", "reject"]


def test_record_conflict_escalates(tmp_path, app_db, cases) -> None:
    h = Harness(tmp_path, app_db, cases, ScriptedChatModel(responses=investigation(), proposals=[dict(PROPOSAL)]))
    h.start()
    # Someone else compensates the order while the proposal waits for review.
    cases.record_action(A01_CASE, ("KOP",), {"type": "full_refund", "amount": 27.84, "proposal_version": 1,
                                             "approved_by": "someone-else"}, "other-key")
    final = h.resume({"decision": "approve", "reviewer": "sup-kop-01"})
    assert final["status"] == "escalated" and final["outcome_reason"].startswith("conflict_at_record_time")


def test_graph_renders_as_mermaid(tmp_path, app_db, cases) -> None:
    h = Harness(tmp_path, app_db, cases, ScriptedChatModel(responses=[], proposals=[]))
    diagram = h.app.get_graph().draw_mermaid()
    assert "human_review" in diagram and "check_evidence" in diagram


def test_fault_plan_after_and_count() -> None:
    plan = FaultPlan()
    plan.set([ModelFault(mode="inference_down", node="propose", after=1, count=1)])
    plan.before_call("investigate")
    plan.before_call("propose")
    with pytest.raises(InferenceUnavailable):
        plan.before_call("propose")
    plan.before_call("propose")
    assert plan.describe() == []

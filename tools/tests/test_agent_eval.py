"""The module 2 agent evaluation against a scripted chat model."""

from __future__ import annotations

from typing import Any

import httpx
from langchain_core.messages import AIMessage

from taco_shared.case_api_client import CaseApiClient
from taco_shared.config import InferenceSettings
from taco_shared.paths import GENERATED_DIR, load_json
from taco_shared.teaching_agent.scenarios import build_tools, find_scenario
from taco_shared.testing import ScriptedChatModel, tool_call
from taco_tools.agent_eval import run_agent_scenarios

A01_PROPOSAL = {
    "case_id": "CASE-10431", "order_id": "TA-10431", "complaint_category": "missing_items",
    "resolution_type": "partial_refund", "refund_amount": 8.5, "affected_items": ["Carnitas Taco"],
    "policy_rule_ids": ["R1"], "requires_human_approval": False, "customer_message": "Sorry about the tacos.",
    "justification": "Two of three Carnitas Tacos missing; R1.", "policy_version": "2026.09",
    "evidence_refs": [{"tool": "get_order", "record_id": "order:TA-10431"},
                      {"tool": "get_resolution_policy", "record_id": "policy:2026.09#R1"}],
    "clarification_or_escalation_reason": "",
}


def case_client() -> CaseApiClient:
    cases = {case["case_id"]: case for case in load_json(GENERATED_DIR / "cases.json")}

    def handler(request: httpx.Request) -> httpx.Response:
        case = cases.get(request.url.path.rsplit("/", 1)[-1])
        scope = set(request.headers.get("X-Store-Scope", "").split(","))
        if case is None or case["store_id"] not in scope:
            return httpx.Response(404, json={"detail": "case not found"})
        return httpx.Response(200, json=case)

    return CaseApiClient("http://case-api", transport=httpx.MockTransport(handler))


def full_investigation() -> list[AIMessage]:
    return [
        AIMessage(content="", tool_calls=[tool_call("get_order", {"order_id": "TA-10431"}, "c1")]),
        AIMessage(content="", tool_calls=[tool_call("get_prior_resolutions", {"lookback_days": 90}, "c2"),
                                          tool_call("get_resolution_policy", {"complaint_category": "missing_items"}, "c3")]),
        AIMessage(content="", tool_calls=[tool_call("get_case_status", {}, "c4")]),
        AIMessage(content="Two of three Carnitas Tacos are missing; R1 applies."),
    ]


def test_eval_grades_a_scenario(app_db, monkeypatch) -> None:
    settings = InferenceSettings(base_url="http://unused/v1", model="scripted", app_db=str(app_db.path), max_tool_calls=8)
    cases = case_client()
    scenario = find_scenario("A01")
    monkeypatch.setattr(run_agent_scenarios, "build_chat_model",
                        lambda s: ScriptedChatModel(responses=full_investigation(), proposals=[dict(A01_PROPOSAL)]))
    monkeypatch.setattr(run_agent_scenarios, "build_tools", lambda s, ctx: build_tools(s, ctx, app_db, cases))
    orders: dict[str, Any] = {o["order_id"]: o for o in load_json(GENERATED_DIR / "orders.json")}
    row = run_agent_scenarios.run_scenario(settings, scenario, orders, None, False)
    assert all(row["tools"][key] for key in run_agent_scenarios.TOOL_FLAGS)
    assert row["grades"]["passed"], row["grades"]["issues"]

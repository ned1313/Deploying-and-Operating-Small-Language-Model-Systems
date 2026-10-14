"""The module 2 teaching agent's driver loop, run against a scripted chat model."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage

from taco_shared.config import InferenceSettings
from taco_shared.db import AppDatabase
from taco_shared.teaching_agent.loop import run_chat, run_investigation
from taco_shared.teaching_agent.render import StepPrinter
from taco_shared.teaching_agent.scenarios import build_tools, context_for, find_scenario
from taco_shared.testing import ScriptedChatModel
from taco_shared.testing import tool_call as call

A01_PROPOSAL = {
    "case_id": "CASE-10431", "order_id": "TA-10431", "complaint_category": "missing_items",
    "resolution_type": "partial_refund", "refund_amount": 8.5, "affected_items": ["Carnitas Taco"],
    "policy_rule_ids": ["R1"], "requires_human_approval": False, "customer_message": "Sorry about the tacos.",
    "justification": "Two of three Carnitas Tacos missing; R1.", "policy_version": "2026.09",
    "evidence_refs": [{"tool": "get_order", "record_id": "order:TA-10431"},
                      {"tool": "get_resolution_policy", "record_id": "policy:2026.09#R1"}],
    "clarification_or_escalation_reason": "",
}


@pytest.fixture
def settings(app_db: AppDatabase) -> InferenceSettings:
    return InferenceSettings(base_url="http://unused/v1", model="scripted", app_db=str(app_db.path), max_tool_calls=8)


def a01_tools(settings, app_db, case_client, actor: str = "rep-kop-01"):
    scenario = find_scenario("A01")
    return scenario, build_tools(settings, context_for(scenario["intake"], actor), app_db, case_client)


def full_investigation() -> list[AIMessage]:
    return [
        AIMessage(content="", tool_calls=[call("get_order", {"order_id": "TA-10431"}, "c1")]),
        AIMessage(content="", tool_calls=[call("get_prior_resolutions", {"lookback_days": 90}, "c2"),
                                          call("get_resolution_policy", {"complaint_category": "missing_items"}, "c3")]),
        AIMessage(content="", tool_calls=[call("get_case_status", {}, "c4")]),
        AIMessage(content="Two of three Carnitas Tacos are missing; R1 applies."),
    ]


def test_full_investigation_is_accepted(settings, app_db, case_client, capsys) -> None:
    scenario, tools = a01_tools(settings, app_db, case_client)
    model = ScriptedChatModel(responses=full_investigation(), proposals=[dict(A01_PROPOSAL)])
    result = run_investigation(model, tools, scenario["intake"], settings, on_step=StepPrinter())
    names = [c["name"] for c in result.transcript.tool_calls]
    assert names == ["get_order", "get_prior_resolutions", "get_resolution_policy", "get_case_status"]
    assert result.report.accepted, result.report.to_dict()
    assert not result.transcript.limit_hit
    output = capsys.readouterr().out
    assert "tool_call get_order" in output and "ACCEPTED" in output


def test_no_tool_calls_is_rejected_by_validator(settings, app_db, case_client) -> None:
    scenario, tools = a01_tools(settings, app_db, case_client)
    model = ScriptedChatModel(responses=[AIMessage(content="Refund the tacos.")], proposals=[dict(A01_PROPOSAL)])
    result = run_investigation(model, tools, scenario["intake"], settings)
    assert result.transcript.tool_calls == []
    violations = " ".join(result.report.business_rule_violations)
    assert "not returned by get_order" in violations and "policy was not retrieved" in violations


def test_invalid_arguments_and_unknown_tool_return_errors(settings, app_db, case_client) -> None:
    scenario, tools = a01_tools(settings, app_db, case_client)
    model = ScriptedChatModel(responses=[
        AIMessage(content="", tool_calls=[call("get_order", {"order_id": "10431"}, "c1")]),
        AIMessage(content="", tool_calls=[call("issue_refund", {"amount": 50}, "c2")]),
        AIMessage(content="Could not verify the order."),
    ], proposals=[dict(A01_PROPOSAL)])
    result = run_investigation(model, tools, scenario["intake"], settings)
    results = result.transcript.tool_results
    assert results[0]["error_kind"] == "invalid_arguments"
    assert results[1]["status"] == "error"
    assert not result.report.accepted


def test_out_of_scope_actor_gets_not_found(settings, app_db, case_client) -> None:
    scenario, tools = a01_tools(settings, app_db, case_client, actor="rep-wg-01")
    model = ScriptedChatModel(responses=full_investigation(), proposals=[dict(A01_PROPOSAL)])
    result = run_investigation(model, tools, scenario["intake"], settings)
    order_step = result.transcript.tool_results[0]
    assert order_step["error_kind"] == "not_found"
    assert any("not returned by get_order" in v for v in result.report.business_rule_violations)


def test_limit_middleware_ends_the_run(settings, app_db, case_client) -> None:
    scenario, tools = a01_tools(settings, app_db, case_client)
    limited = replace(settings, max_tool_calls=2)
    model = ScriptedChatModel(responses=full_investigation(), proposals=[dict(A01_PROPOSAL)])
    result = run_investigation(model, tools, scenario["intake"], limited)
    assert result.transcript.limit_hit
    assert any(step["kind"] == "limit" for step in result.transcript.steps)
    assert result.report is not None


def test_finalize_without_structured_output_and_parse_failure(settings, app_db, case_client) -> None:
    scenario, tools = a01_tools(settings, app_db, case_client)
    prompt_only = replace(settings, structured_output="none")
    model = ScriptedChatModel(responses=[*full_investigation(), AIMessage(content="```json\n" + json.dumps(A01_PROPOSAL) + "\n```")])
    result = run_investigation(model, tools, scenario["intake"], prompt_only)
    assert result.proposal == A01_PROPOSAL and result.report.accepted
    model = ScriptedChatModel(responses=full_investigation(), proposals=[ValueError("truncated")])
    result = run_investigation(model, tools, scenario["intake"], settings)
    assert result.proposal is None and "could not parse proposal" in result.report.schema_errors[0]


def test_chat_mode_has_no_tools() -> None:
    model = ScriptedChatModel(responses=[AIMessage(content="Offer a $10 credit.")])
    result = run_chat(model, find_scenario("A01")["intake"])
    assert result.answer == "Offer a $10 credit." and result.transcript.tool_calls == []

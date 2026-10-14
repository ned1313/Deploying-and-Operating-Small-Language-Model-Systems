"""Driver loop, teaching script, and evaluation, run against a scripted chat model."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from langchain_core.messages import AIMessage

from agent.loop import run_chat, run_investigation
from agent.render import StepPrinter
from agent.scenarios import build_tools, context_for, find_scenario
from eval import run_agent_scenarios

from .conftest import A01_PROPOSAL, ScriptedChatModel, call

M2 = Path(__file__).resolve().parents[1]


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


def test_eval_grades_a_scenario(settings, app_db, case_client, monkeypatch) -> None:
    scenario = find_scenario("A01")
    monkeypatch.setattr(run_agent_scenarios, "build_chat_model",
                        lambda s: ScriptedChatModel(responses=full_investigation(), proposals=[dict(A01_PROPOSAL)]))
    monkeypatch.setattr(run_agent_scenarios, "build_tools", lambda s, ctx: build_tools(s, ctx, app_db, case_client))
    orders = {o["order_id"]: o for o in json.loads((Path(run_agent_scenarios.GENERATED_DIR) / "orders.json").read_text())}
    row = run_agent_scenarios.run_scenario(settings, scenario, orders, None, False)
    assert all(row["tools"][key] for key in run_agent_scenarios.TOOL_FLAGS)
    assert row["grades"]["passed"], row["grades"]["issues"]


def test_script_reports_unreachable_endpoint(tmp_path: Path) -> None:
    env = {"INFERENCE_BASE_URL": "http://127.0.0.1:9/v1", "INFERENCE_MODEL": "x", "INFERENCE_TIMEOUT_SECONDS": "5",
           "INFERENCE_API_KEY_FILE": str(tmp_path / "none"), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    completed = subprocess.run([sys.executable, str(M2 / "examples" / "basic_agent.py"), "--mode", "chat",
                                "--scenario", "A01"], capture_output=True, text=True, env=env, timeout=120)
    assert completed.returncode == 1
    assert "not a model-quality problem" in completed.stdout

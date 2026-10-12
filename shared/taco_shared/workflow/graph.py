"""The complaint-resolution graph: bounded investigation, proposal, validation, human approval, record.

Module 2's loop maps onto these nodes: the `create_agent` model/tool cycle becomes `investigate` +
`tools`, finalize becomes `propose`, and the validator becomes `validate`. Module 3 adds what the loop
lacked: deterministic routing (clarify, escalate), budgets, a durable approval interrupt, and an
idempotent record step.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import openai
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..agent.faults import FaultPlan, InferenceUnavailable
from ..agent.prompts import (CLARIFY_MISSING_ORDER, CLARIFY_ORDER_NOT_FOUND, INVESTIGATE_PROMPT, correction_feedback,
                             intake_message, missing_evidence_nudge, revision_feedback)
from ..agent.propose import generate_proposal, usage_of
from ..agent.tools import build_investigation_tools, error_result
from ..case_api_client import CaseApiClient, CaseApiConflict
from ..context import AgentContext
from ..db import AppDatabase
from ..errors import describe_error
from ..policy import Policy
from ..proposal import MONETARY_TYPES, REQUIRED_TOOLS, Evidence, validate_proposal
from .state import CaseState

ORDER_ID = re.compile(r"\bTA-\d{5}\b")
Observer = Callable[[str, dict[str, Any]], None]


def _int_env(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


@dataclass(frozen=True)
class WorkflowSettings:
    max_tool_calls: int = 8
    max_nudges: int = 1
    max_corrections: int = 1
    run_timeout_seconds: float = 90.0
    structured_output: str = "json_schema"

    @classmethod
    def from_env(cls) -> WorkflowSettings:
        return cls(max_tool_calls=_int_env("AGENT_MAX_TOOL_CALLS", 8),
                   max_nudges=_int_env("WORKFLOW_MAX_NUDGES", 1),
                   max_corrections=_int_env("WORKFLOW_MAX_CORRECTIONS", 1),
                   run_timeout_seconds=float(os.environ.get("WORKFLOW_RUN_TIMEOUT_SECONDS") or 90),
                   structured_output=os.environ.get("INFERENCE_STRUCTURED_OUTPUT") or "json_schema")


@dataclass
class WorkflowDeps:
    model: BaseChatModel
    db: AppDatabase
    cases: CaseApiClient
    policy: Policy
    settings: WorkflowSettings = field(default_factory=WorkflowSettings)
    faults: FaultPlan = field(default_factory=FaultPlan)
    observer: Observer | None = None

    def emit(self, event: str, **data: Any) -> None:
        if self.observer:
            self.observer(event, data)


def context_of(state: CaseState) -> AgentContext:
    intake = state["intake"]
    return AgentContext(actor_id=state["actor_id"], store_ids=tuple(state["store_ids"]),
                        customer_id=intake["customer_id"], case_id=intake["case_id"],
                        complaint_date=intake["complaint_date"])


def evidence_from(state: CaseState) -> Evidence:
    evidence = Evidence()
    for entry in state.get("evidence_log", []):
        evidence.add(entry["tool"], entry["content"])
    return evidence


def case_status(state: CaseState, interrupted: bool) -> str:
    return "pending_review" if interrupted else state.get("status", "new")


def _parse(content: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
    except ValueError:
        parsed = None
    return parsed if isinstance(parsed, dict) else {"status": "error", "error_kind": "tool_error", "message": str(content)}


def _signature(name: str, args: Any) -> str:
    return json.dumps([name, args], sort_keys=True, default=str)


def _past_deadline(config: RunnableConfig) -> bool:
    deadline = (config.get("configurable") or {}).get("deadline")
    return deadline is not None and time.time() > deadline


def _call_model(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except (openai.APIConnectionError, openai.InternalServerError, openai.RateLimitError) as error:
        raise InferenceUnavailable(describe_error(error)["message"]) from error


def build_graph(deps: WorkflowDeps) -> StateGraph:
    settings = deps.settings

    def tools_for(state: CaseState):
        return build_investigation_tools(context_of(state), deps.db, deps.cases, deps.policy)

    def timed(name: str, fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        def node(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                return fn(state, config)
            finally:
                deps.emit("node", node=name, seconds=time.perf_counter() - started)
        node.__name__ = name
        return node

    def intake(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
        if not ORDER_ID.search(state["intake"]["complaint"]):
            return {"next_step": "clarify", "outcome_reason": "missing_order_id",
                    "clarification_question": CLARIFY_MISSING_ORDER}
        return {"next_step": "investigate", "status": "investigating", "outcome_reason": None,
                "clarification_question": None}

    def investigate(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
        if _past_deadline(config):
            return {"next_step": "escalate", "outcome_reason": "timeout"}
        deps.faults.before_call("investigate")
        tools = tools_for(state)
        messages = [SystemMessage(INVESTIGATE_PROMPT), HumanMessage(intake_message(state["intake"])),
                    *state.get("messages", [])]
        started = time.perf_counter()
        reply: AIMessage = _call_model(lambda: deps.model.bind_tools(tools).invoke(messages))
        elapsed = time.perf_counter() - started
        previous = next((call for m in reversed(state.get("messages", [])) if isinstance(m, AIMessage)
                         for call in m.tool_calls), None)
        reply, injected = deps.faults.after_investigate(reply, previous)
        usage = usage_of(reply)
        deps.emit("model", node="investigate", seconds=elapsed, **usage)
        step = {"kind": "model", "node": "investigate", "elapsed_seconds": elapsed,
                "content": reply.content if isinstance(reply.content, str) else json.dumps(reply.content),
                "tool_calls": [{"name": c["name"], "args": c["args"]} for c in reply.tool_calls],
                "invalid_tool_calls": [{"name": c.get("name"), "error": c.get("error")} for c in reply.invalid_tool_calls],
                "finish_reason": (reply.response_metadata or {}).get("finish_reason"), "injected_fault": injected,
                **usage}
        has_calls = bool(reply.tool_calls or reply.invalid_tool_calls)
        return {"messages": [reply], "steps": [step], "next_step": "tools" if has_calls else "check_evidence"}

    def tools(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
        last: AIMessage = state["messages"][-1]
        tool_map = {t.name: t for t in tools_for(state)}
        seen = {e["signature"] for e in state.get("evidence_log", []) if e["executed"]}
        used, repeated = state.get("tool_calls_used", 0), state.get("repeated_calls", 0)
        replies, log, steps = [], [], []
        for call in last.tool_calls:
            name, signature = call["name"], _signature(call["name"], call["args"])
            executed, retries, started = False, 0, time.perf_counter()
            if used >= settings.max_tool_calls:
                content = error_result("budget_exhausted", "Tool-call budget exhausted; summarize what you found.")
            elif signature in seen:
                used, repeated = used + 1, repeated + 1
                content = error_result("duplicate_call", f"You already called {name} with these arguments. "
                                                         "Use the earlier result instead of calling it again.")
            elif name not in tool_map:
                used += 1
                content = error_result("unknown_tool", f"There is no tool named {name}.")
            else:
                used, executed = used + 1, True
                before = deps.cases.retries_used
                content = str(tool_map[name].invoke(call).content)
                retries = deps.cases.retries_used - before
                seen.add(signature)
            result = _parse(content)
            elapsed = time.perf_counter() - started
            deps.emit("tool", tool=name, status=result.get("status", "error"), error_kind=result.get("error_kind"),
                      seconds=elapsed, retries=retries)
            replies.append(ToolMessage(content=content, tool_call_id=call["id"], name=name))
            log.append({"tool": name, "args": call["args"], "content": content, "signature": signature,
                        "executed": executed, "retries": retries})
            steps.append({"kind": "tool", "name": name, "elapsed_seconds": elapsed, "status": result.get("status"),
                          "error_kind": result.get("error_kind"), "record_id": result.get("record_id"),
                          "retries": retries, "executed": executed})
        for bad in last.invalid_tool_calls:
            used += 1
            content = error_result("invalid_arguments", f"Could not parse the tool arguments: {bad.get('error')}")
            replies.append(ToolMessage(content=content, tool_call_id=bad.get("id") or "invalid", name=bad.get("name") or "unknown"))
            log.append({"tool": bad.get("name") or "unknown", "args": bad.get("args"), "content": content,
                        "signature": "invalid", "executed": False, "retries": 0})
            steps.append({"kind": "tool", "name": bad.get("name"), "elapsed_seconds": 0.0, "status": "error",
                          "error_kind": "invalid_arguments", "record_id": None, "retries": 0, "executed": False})
        next_step = "check_evidence" if used >= settings.max_tool_calls else "investigate"
        return {"messages": replies, "evidence_log": log, "steps": steps, "tool_calls_used": used,
                "repeated_calls": repeated, "next_step": next_step}

    def check_evidence(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
        log = state.get("evidence_log", [])
        results = [(e["tool"], _parse(e["content"])) for e in log if e["executed"]]
        ok = {tool for tool, r in results if r.get("status") == "ok"}
        case_results = [r for tool, r in results if tool == "get_case_status"]
        if case_results and case_results[-1].get("error_kind") == "unavailable":
            return {"next_step": "escalate", "outcome_reason": "dependency_unavailable"}
        order_results = [r for tool, r in results if tool == "get_order"]
        if order_results and "get_order" not in ok and all(r.get("error_kind") == "not_found" for r in order_results):
            return {"next_step": "clarify", "outcome_reason": "order_not_found",
                    "clarification_question": CLARIFY_ORDER_NOT_FOUND}
        missing = [tool for tool in REQUIRED_TOOLS if tool not in ok]
        if not missing:
            return {"next_step": "propose"}
        nudges = state.get("nudges_used", 0)
        if nudges < settings.max_nudges and state.get("tool_calls_used", 0) < settings.max_tool_calls:
            step = {"kind": "nudge", "missing": missing}
            return {"next_step": "investigate", "nudges_used": nudges + 1, "steps": [step],
                    "messages": [HumanMessage(missing_evidence_nudge(missing))]}
        return {"next_step": "escalate", "outcome_reason": f"missing_evidence: {', '.join(missing)}"}

    def propose(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
        if _past_deadline(config):
            return {"next_step": "escalate", "outcome_reason": "timeout"}
        deps.faults.before_call("propose")
        attempt = _call_model(lambda: generate_proposal(deps.model, state["intake"], state.get("messages", []),
                                                        settings.structured_output, state.get("feedback")))
        attempt, injected = deps.faults.after_propose(attempt)
        deps.emit("model", node="propose", seconds=attempt.step["elapsed_seconds"],
                  input_tokens=attempt.step.get("input_tokens"), output_tokens=attempt.step.get("output_tokens"))
        version = state.get("proposal_version", 0) + 1
        step = {**attempt.step, "node": "propose", "proposal_version": version, "injected_fault": injected,
                "error": attempt.error}
        return {"proposal": attempt.proposal, "proposal_raw": attempt.raw, "proposal_error": attempt.error,
                "proposal_version": version, "feedback": None, "steps": [step], "next_step": "validate"}

    def validate(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
        proposal = state.get("proposal")
        intake_data = state["intake"]
        report = validate_proposal(proposal if proposal is not None else state.get("proposal_raw"),
                                   evidence_from(state), intake_data["complaint_date"])
        if state.get("proposal_error"):
            report.schema_errors.insert(0, f"could not parse proposal: {state['proposal_error']}")
        if isinstance(proposal, dict) and proposal.get("case_id") != intake_data["case_id"]:
            report.business_rule_violations.append(f"case_id should be {intake_data['case_id']}")
        update: dict[str, Any] = {"validation": report.to_dict(),
                                  "steps": [{"kind": "validate", "proposal_version": state.get("proposal_version"),
                                             **report.to_dict()}]}
        if report.accepted:
            return {**update, "next_step": "human_review", "status": "pending_review"}
        corrections = state.get("corrections_used", 0)
        if corrections < settings.max_corrections:
            problems = report.schema_errors + report.business_rule_violations
            return {**update, "next_step": "propose", "corrections_used": corrections + 1,
                    "feedback": correction_feedback(problems)}
        return {**update, "next_step": "escalate", "outcome_reason": "validation_failed"}

    def human_review(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
        # Nothing before interrupt() may have side effects: the node runs again from the top on resume.
        decision = interrupt({"case_id": state["intake"]["case_id"], "proposal_version": state["proposal_version"],
                              "proposal": state["proposal"]})
        review = {"decision": decision["decision"], "reviewer": decision["reviewer"], "note": decision.get("note"),
                  "proposal_version": state["proposal_version"], "at": decision.get("at")}
        if decision["decision"] == "approve":
            return {"reviews": [review], "status": "approved", "next_step": "record"}
        if decision["decision"] == "revise":
            return {"reviews": [review], "status": "revising", "next_step": "propose",
                    "feedback": revision_feedback(decision.get("note") or "")}
        return {"reviews": [review], "next_step": "rejected"}

    def record(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
        proposal, version = state["proposal"], state["proposal_version"]
        case_id = state["intake"]["case_id"]
        monetary = proposal["resolution_type"] in MONETARY_TYPES
        action = {"type": proposal["resolution_type"],
                  "amount": round(float(proposal["refund_amount"]), 2) if monetary else 0.0,
                  "proposal_version": version, "approved_by": state["reviews"][-1]["reviewer"]}
        key = f"{case_id}:v{version}"
        before = deps.cases.retries_used
        try:
            result = deps.cases.record_action(case_id, tuple(state["store_ids"]), action, key)
        except CaseApiConflict as conflict:
            return {"status": "escalated", "outcome_reason": f"conflict_at_record_time: {conflict.detail}",
                    "steps": [{"kind": "record", "idempotency_key": key, "conflict": conflict.detail}]}
        retries = deps.cases.retries_used - before
        return {"status": "recorded", "outcome_reason": None, "action": result["action"],
                "action_replayed": result["replayed"],
                "steps": [{"kind": "record", "idempotency_key": key, "action": result["action"],
                           "replayed": result["replayed"], "retries": retries}]}

    def clarify(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
        return {"status": "needs_clarification", "steps": [{"kind": "clarify", "reason": state.get("outcome_reason")}]}

    def escalate(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
        return {"status": "escalated", "steps": [{"kind": "escalate", "reason": state.get("outcome_reason")}]}

    def rejected(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
        return {"status": "rejected", "outcome_reason": "rejected_by_reviewer", "steps": [{"kind": "rejected"}]}

    def route(state: CaseState) -> str:
        return state["next_step"]

    graph = StateGraph(CaseState)
    for name, fn in (("intake", intake), ("investigate", investigate), ("tools", tools),
                     ("check_evidence", check_evidence), ("propose", propose), ("validate", validate),
                     ("human_review", human_review), ("record", record), ("clarify", clarify),
                     ("escalate", escalate), ("rejected", rejected)):
        graph.add_node(name, timed(name, fn))
    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", route, ["clarify", "investigate"])
    graph.add_conditional_edges("investigate", route, ["tools", "check_evidence", "escalate"])
    graph.add_conditional_edges("tools", route, ["investigate", "check_evidence"])
    graph.add_conditional_edges("check_evidence", route, ["investigate", "propose", "clarify", "escalate"])
    graph.add_conditional_edges("propose", route, ["validate", "escalate"])
    graph.add_conditional_edges("validate", route, ["human_review", "propose", "escalate"])
    graph.add_conditional_edges("human_review", route, ["record", "propose", "rejected"])
    for terminal in ("record", "clarify", "escalate", "rejected"):
        graph.add_edge(terminal, END)
    return graph

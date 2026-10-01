"""Module 2 driver loop around a LangChain `create_agent` agent.

The agent runs the model/tool cycle. This loop streams that run so every step can be shown, records a
transcript, then makes a separate structured-output call for the proposal and validates it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from taco_shared.agent.prompts import CHAT_PROMPT, FINALIZE_PROMPT, INVESTIGATE_PROMPT, finalize_instruction, intake_message
from taco_shared.config import InferenceSettings
from taco_shared.jsonutil import parse_json_output
from taco_shared.proposal import Evidence, ValidationReport, load_schema_v2, validate_proposal

TOOL_MARKUP = re.compile(r"<tool_call>|<\|python_tag\|>|^\s*\{\s*\"(name|function)\"\s*:", re.MULTILINE)
StepCallback = Callable[[dict[str, Any]], None]


@dataclass
class Transcript:
    steps: list[dict[str, Any]] = field(default_factory=list)
    messages: list[BaseMessage] = field(default_factory=list)
    evidence: Evidence = field(default_factory=Evidence)
    limit_hit: bool = False

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return [call for step in self.steps if step["kind"] == "model" for call in step["tool_calls"]]

    @property
    def tool_results(self) -> list[dict[str, Any]]:
        return [step for step in self.steps if step["kind"] == "tool"]

    def to_dict(self) -> dict[str, Any]:
        return {"steps": self.steps, "limit_hit": self.limit_hit}


@dataclass
class InvestigationResult:
    mode: str
    transcript: Transcript
    answer: str | None = None
    proposal: dict[str, Any] | None = None
    proposal_raw: str | None = None
    proposal_error: str | None = None
    report: ValidationReport | None = None
    finalize_step: dict[str, Any] | None = None
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "answer": self.answer, "proposal": self.proposal,
                "proposal_raw": self.proposal_raw, "proposal_error": self.proposal_error,
                "validation": self.report.to_dict() if self.report else None,
                "finalize": self.finalize_step, "elapsed_seconds": self.elapsed_seconds,
                "transcript": self.transcript.to_dict()}


def _usage(message: AIMessage) -> dict[str, Any]:
    usage = message.usage_metadata or {}
    return {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}


def _model_step(message: AIMessage, elapsed: float) -> dict[str, Any]:
    content = message.content if isinstance(message.content, str) else json.dumps(message.content)
    return {
        "kind": "model", "elapsed_seconds": elapsed, "content": content,
        "tool_calls": [{"id": c.get("id"), "name": c["name"], "args": c["args"]} for c in message.tool_calls],
        "invalid_tool_calls": [{"id": c.get("id"), "name": c.get("name"), "args": c.get("args"),
                                "error": c.get("error")} for c in message.invalid_tool_calls],
        "tool_markup_in_content": bool(TOOL_MARKUP.search(content or "")),
        "finish_reason": (message.response_metadata or {}).get("finish_reason"),
        **_usage(message),
    }


def _tool_step(message: ToolMessage, elapsed: float) -> dict[str, Any]:
    content = message.content if isinstance(message.content, str) else json.dumps(message.content)
    try:
        parsed = json.loads(content)
    except ValueError:
        parsed = {"status": "error", "error_kind": "tool_error", "message": content}
    return {"kind": "tool", "elapsed_seconds": elapsed, "name": message.name, "tool_call_id": message.tool_call_id,
            "status": parsed.get("status", "error"), "error_kind": parsed.get("error_kind"),
            "record_id": parsed.get("record_id"), "result": parsed}


def run_chat(model: BaseChatModel, intake: dict[str, Any]) -> InvestigationResult:
    started = time.perf_counter()
    reply = model.invoke([SystemMessage(CHAT_PROMPT), HumanMessage(intake["complaint"])])
    transcript = Transcript(messages=[reply])
    transcript.steps.append(_model_step(reply, time.perf_counter() - started))
    return InvestigationResult(mode="chat", transcript=transcript, answer=str(reply.content),
                               elapsed_seconds=time.perf_counter() - started)


def run_investigation(model: BaseChatModel, tools: list[BaseTool], intake: dict[str, Any],
                      settings: InferenceSettings, on_step: StepCallback | None = None) -> InvestigationResult:
    started = time.perf_counter()
    transcript = Transcript()
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=INVESTIGATE_PROMPT,
        middleware=[ToolCallLimitMiddleware(run_limit=settings.max_tool_calls, exit_behavior="end")],
        # Module 3 adds a checkpointer, routing, retry budgets, and a human-approval interrupt.
    )
    last = time.perf_counter()
    for update in agent.stream({"messages": [HumanMessage(intake_message(intake))]}, stream_mode="updates"):
        for node, delta in update.items():
            delta = delta or {}
            if node not in {"model", "tools"}:
                # Middleware nodes: the limit middleware ends the run with jump_to="end" and its own messages.
                if delta.get("jump_to") == "end":
                    transcript.limit_hit = True
                    transcript.messages.extend(delta.get("messages", []))
                    step = {"kind": "limit", "elapsed_seconds": 0.0,
                            "message": next((str(m.content) for m in delta.get("messages", [])
                                             if isinstance(m, AIMessage)), "tool-call limit reached")}
                    transcript.steps.append(step)
                    if on_step:
                        on_step(step)
                continue
            for message in delta.get("messages", []):
                now = time.perf_counter()
                if isinstance(message, AIMessage):
                    step = _model_step(message, now - last)
                elif isinstance(message, ToolMessage):
                    step = _tool_step(message, now - last)
                    transcript.evidence.add(message.name or "", step["result"])
                else:
                    continue
                last = now
                transcript.messages.append(message)
                transcript.steps.append(step)
                if on_step:
                    on_step(step)

    result = InvestigationResult(mode="tools", transcript=transcript)
    finalize(model, intake, settings, result, on_step)
    result.elapsed_seconds = time.perf_counter() - started
    return result


def finalize(model: BaseChatModel, intake: dict[str, Any], settings: InferenceSettings,
             result: InvestigationResult, on_step: StepCallback | None = None) -> None:
    schema = load_schema_v2()
    guided = settings.structured_output == "json_schema"
    messages = [SystemMessage(FINALIZE_PROMPT), HumanMessage(intake_message(intake)), *result.transcript.messages,
                HumanMessage(finalize_instruction(None if guided else schema))]
    started = time.perf_counter()
    if guided:
        structured = model.with_structured_output(schema, method="json_schema", strict=True, include_raw=True)
        output = structured.invoke(messages)
        raw: AIMessage = output["raw"]
        result.proposal_raw = str(raw.content)
        if output.get("parsing_error") is not None:
            result.proposal_error = f"{type(output['parsing_error']).__name__}: {output['parsing_error']}"
        else:
            result.proposal = output["parsed"]
    else:
        raw = model.invoke(messages)
        result.proposal_raw = str(raw.content)
        try:
            result.proposal, _ = parse_json_output(result.proposal_raw)
        except ValueError as error:
            result.proposal_error = str(error)
    result.finalize_step = {"kind": "finalize", "mode": settings.structured_output,
                            "elapsed_seconds": time.perf_counter() - started,
                            "finish_reason": (raw.response_metadata or {}).get("finish_reason"), **_usage(raw)}
    result.report = validate_proposal(result.proposal if result.proposal is not None else result.proposal_raw,
                                      result.transcript.evidence, intake["complaint_date"], schema)
    if result.proposal_error:
        result.report.schema_errors.insert(0, f"could not parse proposal: {result.proposal_error}")
    if on_step:
        on_step({**result.finalize_step, "proposal": result.proposal, "report": result.report})

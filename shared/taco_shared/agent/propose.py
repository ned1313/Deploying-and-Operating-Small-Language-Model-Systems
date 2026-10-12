"""The proposal call: one structured-output request with no tools, on the investigation history.

Used by the module 2 driver loop (finalize) and the module 3 workflow (propose node).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from ..jsonutil import parse_json_output
from ..proposal import load_schema_v2
from .prompts import FINALIZE_PROMPT, finalize_instruction, intake_message


@dataclass
class ProposalAttempt:
    proposal: dict[str, Any] | None
    raw: str | None
    error: str | None
    step: dict[str, Any]


def usage_of(message: AIMessage) -> dict[str, Any]:
    usage = message.usage_metadata or {}
    return {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}


def generate_proposal(model: BaseChatModel, intake: dict[str, Any], history: list[BaseMessage],
                      structured_output: str = "json_schema", feedback: str | None = None) -> ProposalAttempt:
    """`feedback` (validation errors or a reviewer note) is added before the final instruction."""
    schema = load_schema_v2()
    guided = structured_output == "json_schema"
    messages: list[BaseMessage] = [SystemMessage(FINALIZE_PROMPT), HumanMessage(intake_message(intake)), *history]
    if feedback:
        messages.append(HumanMessage(feedback))
    messages.append(HumanMessage(finalize_instruction(None if guided else schema)))
    started = time.perf_counter()
    proposal: dict[str, Any] | None = None
    error: str | None = None
    if guided:
        output = model.with_structured_output(schema, method="json_schema", strict=True, include_raw=True).invoke(messages)
        raw: AIMessage = output["raw"]
        if output.get("parsing_error") is not None:
            error = f"{type(output['parsing_error']).__name__}: {output['parsing_error']}"
        else:
            proposal = output["parsed"]
    else:
        raw = model.invoke(messages)
        try:
            proposal, _ = parse_json_output(str(raw.content))
        except ValueError as exc:
            error = str(exc)
    finish_reason = (raw.response_metadata or {}).get("finish_reason")
    if finish_reason == "length":
        proposal, error = None, "output truncated at the token limit (finish_reason=length)"
    step = {"kind": "finalize", "mode": structured_output, "elapsed_seconds": time.perf_counter() - started,
            "finish_reason": finish_reason, **usage_of(raw)}
    return ProposalAttempt(proposal=proposal, raw=str(raw.content), error=error, step=step)

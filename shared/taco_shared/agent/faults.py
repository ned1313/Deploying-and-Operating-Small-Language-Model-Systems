"""Deterministic model fault injection for module 3 failure tests.

The workflow consults a FaultPlan around each model call. Every injected change is labeled
(`injected_fault`) in the workflow steps and logs, so an injected failure is never mistaken for a
model failure. Faults fire on matching calls after skipping `after` of them, `count` times (or until
cleared when `count` is not set).
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Literal

from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from .propose import ProposalAttempt

FaultMode = Literal["inference_down", "malformed_json", "drop_field", "invent_item", "repeat_tool_call",
                    "invalid_tool_args"]
INVENTED_ITEM = "Lobster Taco"


class InferenceUnavailable(RuntimeError):
    """The inference endpoint could not be reached after the model client's retries."""


class ModelFault(BaseModel):
    mode: FaultMode
    node: Literal["investigate", "propose", "any"] = "any"
    after: int = Field(default=0, ge=0)
    count: int | None = Field(default=None, ge=1)


class FaultPlan:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._faults: list[dict[str, Any]] = []

    def set(self, faults: list[ModelFault]) -> None:
        with self._lock:
            self._faults = [{"fault": fault, "seen": 0, "fired": 0} for fault in faults]

    def clear(self) -> None:
        self.set([])

    def describe(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{**item["fault"].model_dump(), "fired": item["fired"]} for item in self._faults]

    def _take(self, node: str, modes: set[str]) -> ModelFault | None:
        with self._lock:
            for item in self._faults:
                fault: ModelFault = item["fault"]
                if fault.mode not in modes or fault.node not in {"any", node}:
                    continue
                item["seen"] += 1
                if item["seen"] <= fault.after:
                    continue
                item["fired"] += 1
                if fault.count is not None and item["fired"] >= fault.count:
                    self._faults.remove(item)
                return fault
        return None

    def before_call(self, node: str) -> None:
        if self._take(node, {"inference_down"}):
            raise InferenceUnavailable("connection refused (injected fault: inference_down)")

    def after_investigate(self, message: AIMessage, previous_call: dict[str, Any] | None) -> tuple[AIMessage, str | None]:
        modes = {"repeat_tool_call"} if previous_call else set()
        if message.tool_calls:
            modes.add("invalid_tool_args")
        fault = self._take("investigate", modes) if modes else None
        if fault is None:
            return message, None
        if fault.mode == "repeat_tool_call":
            call = {**previous_call, "id": f"injected-{uuid.uuid4().hex[:8]}", "type": "tool_call"}
        else:
            call = {**message.tool_calls[0], "args": {"order_id": "10431"}}
        return AIMessage(content="", tool_calls=[call], usage_metadata=message.usage_metadata,
                         response_metadata=message.response_metadata), fault.mode

    def after_propose(self, attempt: ProposalAttempt) -> tuple[ProposalAttempt, str | None]:
        if attempt.proposal is None:
            return attempt, None
        fault = self._take("propose", {"malformed_json", "drop_field", "invent_item"})
        if fault is None:
            return attempt, None
        proposal = dict(attempt.proposal)
        if fault.mode == "malformed_json":
            raw = (attempt.raw or "")[: max(1, len(attempt.raw or "") // 2)]
            return ProposalAttempt(None, raw, "JSONDecodeError: unterminated object (injected fault)", attempt.step), fault.mode
        if fault.mode == "drop_field":
            proposal.pop("policy_version", None)
        else:
            proposal["affected_items"] = [*proposal.get("affected_items", []), INVENTED_ITEM]
        return ProposalAttempt(proposal, attempt.raw, None, attempt.step), fault.mode

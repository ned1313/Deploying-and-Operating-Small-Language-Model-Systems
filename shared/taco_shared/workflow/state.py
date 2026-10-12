"""Workflow state. Approval lives only in `reviews`, written from an authorized reviewer's decision."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class CaseState(TypedDict, total=False):
    request_id: str
    actor_id: str                   # trusted: set by the API from the caller's identity
    store_ids: list[str]            # trusted: derived from the actor, never from the model
    intake: dict[str, Any]          # case_id, customer_id, complaint_date, complaint_channel, complaint
    messages: Annotated[list[BaseMessage], add_messages]
    evidence_log: Annotated[list[dict[str, Any]], operator.add]
    steps: Annotated[list[dict[str, Any]], operator.add]
    reviews: Annotated[list[dict[str, Any]], operator.add]
    tool_calls_used: int
    repeated_calls: int
    nudges_used: int
    corrections_used: int
    next_step: str | None
    feedback: str | None
    proposal: dict[str, Any] | None
    proposal_raw: str | None
    proposal_error: str | None
    proposal_version: int
    validation: dict[str, Any] | None
    action: dict[str, Any] | None
    action_replayed: bool
    status: str
    outcome_reason: str | None
    clarification_question: str | None

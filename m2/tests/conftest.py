"""Test helpers for module 2: a scripted chat model and wiring to a seeded temporary database."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taco_shared.case_api_client import CaseApiClient  # noqa: E402
from taco_shared.config import InferenceSettings  # noqa: E402
from taco_shared.dataset.seed import fixture_digest, write_database  # noqa: E402
from taco_shared.db import AppDatabase  # noqa: E402
from taco_shared.paths import GENERATED_DIR, load_json  # noqa: E402


class ScriptedChatModel(BaseChatModel):
    """Returns pre-scripted AI messages in order; structured output returns scripted proposals."""

    responses: list[AIMessage]
    proposals: list[Any] = []
    calls: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.calls.append(list(messages))
        message = self.responses.pop(0)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":
        return self

    def with_structured_output(self, schema: Any, **kwargs: Any) -> RunnableLambda:
        def respond(messages: Any) -> dict[str, Any]:
            proposal = self.proposals.pop(0)
            if isinstance(proposal, Exception):
                return {"raw": AIMessage(content="{bad"), "parsed": None, "parsing_error": proposal}
            return {"raw": AIMessage(content=json.dumps(proposal)), "parsed": proposal, "parsing_error": None}
        return RunnableLambda(respond)


def call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


@pytest.fixture(scope="session")
def app_db(tmp_path_factory: pytest.TempPathFactory) -> AppDatabase:
    path = tmp_path_factory.mktemp("app") / "taco_app.db"
    write_database(path, fixture_digest())
    return AppDatabase(path)


@pytest.fixture(scope="session")
def case_client() -> CaseApiClient:
    cases = {case["case_id"]: case for case in load_json(GENERATED_DIR / "cases.json")}

    def handler(request: httpx.Request) -> httpx.Response:
        case = cases.get(request.url.path.rsplit("/", 1)[-1])
        scope = set(request.headers.get("X-Store-Scope", "").split(","))
        if case is None or case["store_id"] not in scope:
            return httpx.Response(404, json={"detail": "case not found"})
        return httpx.Response(200, json=case)

    return CaseApiClient("http://case-api", transport=httpx.MockTransport(handler))


@pytest.fixture
def settings(app_db: AppDatabase) -> InferenceSettings:
    return InferenceSettings(base_url="http://unused/v1", model="scripted", app_db=str(app_db.path), max_tool_calls=8)


A01_PROPOSAL = {
    "case_id": "CASE-10431", "order_id": "TA-10431", "complaint_category": "missing_items",
    "resolution_type": "partial_refund", "refund_amount": 8.5, "affected_items": ["Carnitas Taco"],
    "policy_rule_ids": ["R1"], "requires_human_approval": False, "customer_message": "Sorry about the tacos.",
    "justification": "Two of three Carnitas Tacos missing; R1.", "policy_version": "2026.09",
    "evidence_refs": [{"tool": "get_order", "record_id": "order:TA-10431"},
                      {"tool": "get_resolution_policy", "record_id": "policy:2026.09#R1"}],
    "clarification_or_escalation_reason": "",
}

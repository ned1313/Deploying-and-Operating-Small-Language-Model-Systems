"""Test helpers shared by the module test suites: a scripted chat model and the in-process case API."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

# Source checkout first, then the copy of shared/ in the course images.
CASE_API_CANDIDATES = [Path(os.environ.get("TACO_CASE_API_MAIN", "")),
                       Path(__file__).resolve().parents[1] / "services" / "mock_case_api" / "main.py",
                       Path("/opt/shared/services/mock_case_api/main.py")]


class ScriptedChatModel(BaseChatModel):
    """Returns scripted AI messages in order; structured output returns scripted proposals.

    A scripted item that is an Exception is raised instead of returned.
    """

    responses: list[Any]
    proposals: list[Any] = []
    calls: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.calls.append(list(messages))
        message = self.responses.pop(0)
        if isinstance(message, BaseException):
            raise message
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":
        return self

    def with_structured_output(self, schema: Any, **kwargs: Any) -> RunnableLambda:
        def respond(messages: Any) -> dict[str, Any]:
            self.calls.append(list(messages))
            proposal = self.proposals.pop(0)
            if isinstance(proposal, BaseException) and not isinstance(proposal, ValueError):
                raise proposal
            if isinstance(proposal, Exception):
                return {"raw": AIMessage(content="{bad"), "parsed": None, "parsing_error": proposal}
            return {"raw": AIMessage(content=json.dumps(proposal)), "parsed": proposal, "parsing_error": None}
        return RunnableLambda(respond)


def tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


class InProcessTransport(httpx.BaseTransport):
    """Routes an httpx.Client (for example CaseApiClient) through a Starlette TestClient."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        target = request.url.raw_path.decode("ascii")
        response = self.client.request(request.method, target, headers=dict(request.headers), content=request.read())
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)


def load_case_api_module(db_path: Path) -> ModuleType:
    """A fresh import of the mock case API pointed at `db_path` (use with fastapi.testclient.TestClient)."""
    path = next(p for p in CASE_API_CANDIDATES if p.name == "main.py" and p.is_file())
    spec = importlib.util.spec_from_file_location(f"mock_case_api_{abs(hash(db_path))}", path)
    module = importlib.util.module_from_spec(spec)
    # Pydantic resolves the postponed annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    module.DB_PATH = db_path
    return module

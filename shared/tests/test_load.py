"""Module 4 load workloads and request-id forwarding."""

from __future__ import annotations

import httpx2

from taco_shared.agent.model import build_chat_model
from taco_shared.config import InferenceSettings
from taco_shared.load import LOAD_DATASET_VERSION, STORE_REPS, direct_requests, load_cases


def test_load_cases_are_deterministic_and_scoped() -> None:
    first, second = load_cases(), load_cases()
    assert LOAD_DATASET_VERSION == "load-v1"
    assert first == second
    assert len({case["case_id"] for case in first}) == len(first) == 150
    assert all(case["actor_id"] == STORE_REPS[case["store_id"]] for case in first)
    assert all(not case["case_id"].startswith("CASE-10431-") for case in first)


def test_direct_requests_inline_facts_with_json_schema() -> None:
    requests = direct_requests("qwen3-14b", {"chat_template_kwargs": {"enable_thinking": False}})
    assert len(requests) == 22
    body = requests[0]["body"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert '"order"' in body["messages"][1]["content"]


def test_chat_model_forwards_request_id() -> None:
    seen: list[str | None] = []
    model = build_chat_model(InferenceSettings(base_url="http://inference.test/v1", model="m"),
                             request_id=lambda: "req-123")
    client = model.root_client._client
    client._transport = httpx2.MockTransport(lambda request: (seen.append(request.headers.get("X-Request-ID")),
                                                              httpx2.Response(200, json={"data": []}))[1])
    model.root_client.models.list()
    assert seen == ["req-123"]

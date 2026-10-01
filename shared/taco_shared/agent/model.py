"""LangChain chat model factory for the self-hosted, OpenAI-compatible inference endpoint."""

from __future__ import annotations

import time
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from openai import APIError, OpenAI

from ..config import InferenceSettings
from ..errors import describe_error


def build_chat_model(settings: InferenceSettings, max_tokens: int | None = None) -> ChatOpenAI:
    return ChatOpenAI(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
        timeout=settings.timeout_seconds,
        max_retries=0,
        temperature=0,
        seed=42,
        max_tokens=max_tokens or settings.max_output_tokens,
        extra_body=settings.extra_body or None,
        # vLLM is used through Chat Completions; the Responses API is not part of the contract.
        use_responses_api=False,
    )


def check_endpoint(settings: InferenceSettings) -> dict[str, Any]:
    """List served models, confirm the configured one is present, and make one tiny chat call."""
    result: dict[str, Any] = {"base_url": settings.base_url, "model": settings.model, "ok": False}
    client = OpenAI(base_url=settings.base_url, api_key=settings.api_key, timeout=settings.timeout_seconds,
                    max_retries=0)
    try:
        served = [model.model_dump() for model in client.models.list().data]
    except APIError as error:
        result["error"] = {"step": "list models", **describe_error(error)}
        return result
    result["served"] = [{"id": m.get("id"), "max_model_len": m.get("max_model_len"), "root": m.get("root")}
                        for m in served]
    if settings.model not in {m.get("id") for m in served}:
        result["error"] = {"step": "list models", "kind": "configuration",
                           "message": f"INFERENCE_MODEL {settings.model!r} is not served here"}
        return result
    started = time.perf_counter()
    try:
        reply = build_chat_model(settings, max_tokens=20).invoke([HumanMessage("Reply with the single word: ready")])
    except APIError as error:
        result["error"] = {"step": "chat completion", **describe_error(error)}
        return result
    result.update(ok=True, latency_seconds=time.perf_counter() - started, reply=str(reply.content).strip(),
                  usage=reply.usage_metadata)
    return result

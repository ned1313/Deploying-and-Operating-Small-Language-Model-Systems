"""Module 4 additions to the complaint API: bounded run slots, trace config, and deployment versions."""

from __future__ import annotations

from complaint_api.observability import deployment_versions

from .conftest import scripted

REP = {"X-Actor-Id": "rep-kop-01"}


def service():
    from complaint_api import main
    return main.STATE["service"]


def test_queue_timeout_returns_503_with_retry_after(make_api) -> None:
    api = make_api(scripted("CASE-10431"))
    svc = service()
    object.__setattr__(svc.settings, "queue_timeout_seconds", 0.05)
    assert svc._slots.acquire(timeout=1)
    try:
        response = api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)
    finally:
        svc._slots.release()
    assert response.status_code == 503 and response.headers["Retry-After"] == "10"
    assert api.get("/cases/CASE-10431", headers=REP).status_code == 404
    assert api.post("/complaints", json={"scenario_id": "A01"}, headers=REP).status_code == 201
    metrics = api.get("/metrics").text
    assert "taco_workflow_queue_rejected_total" in metrics and "taco_workflow_runs_in_progress" in metrics


def test_run_config_carries_trace_metadata_and_tags(make_api) -> None:
    api = make_api(scripted("CASE-10431"))
    api.post("/complaints", json={"scenario_id": "A01"}, headers={**REP, "X-Request-ID": "req-9"})
    svc = service()
    case = svc.store.get("CASE-10431")
    svc.deps.faults.set([])
    config = svc._run_config(case, "decision", "req-9", "sup-kop-01")
    assert config["run_name"] == "decision"
    assert config["tags"] == ["decision", "scenario:A01"]
    assert config["metadata"]["thread_id"] == case["thread_id"] and config["metadata"]["request_id"] == "req-9"
    assert config["configurable"]["thread_id"] == case["thread_id"]


def test_deployment_versions_are_stable() -> None:
    first = deployment_versions("qwen3-14b", "2026.09")
    assert first == deployment_versions("qwen3-14b", "2026.09")
    assert first["dataset_version"] == "agent-v2" and first["schema_version"] == "proposal.v2"
    assert len(first["prompt_version"]) == 8


def test_unreachable_trace_endpoint_does_not_fail_requests(make_api, monkeypatch) -> None:
    import time

    from langchain_core.tracers.langchain import wait_for_all_tracers
    from langsmith import Client
    from langsmith import utils as ls_utils

    def refuse(*_, **__):
        raise ls_utils.LangSmithConnectionError("connection refused (test)")

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    # Fail exports at once; the SDK's own retries and backoff would otherwise delay the test by ~45 s.
    monkeypatch.setattr(Client, "request_with_retries", refuse)
    monkeypatch.setattr(Client, "_send_multipart_req", refuse)
    ls_utils.get_env_var.cache_clear()
    try:
        api = make_api(scripted("CASE-10431"))
        started = time.perf_counter()
        response = api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)
        assert response.status_code == 201 and response.json()["status"] == "pending_review"
        assert time.perf_counter() - started < 5
        wait_for_all_tracers()
    finally:
        monkeypatch.undo()
        ls_utils.get_env_var.cache_clear()

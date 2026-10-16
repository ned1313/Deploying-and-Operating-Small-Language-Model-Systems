"""App test wiring: the complaint API with a scripted model, real checkpoints, and the in-process case API."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taco_shared.case_api_client import CaseApiClient  # noqa: E402
from taco_shared.dataset.seed import fixture_digest, write_database  # noqa: E402
from taco_shared.db import AppDatabase  # noqa: E402
from taco_shared.policy import load_policy  # noqa: E402
from taco_shared.testing import InProcessTransport, ScriptedChatModel, load_case_api_module, tool_call  # noqa: E402
from taco_shared.workflow import WorkflowDeps, WorkflowSettings  # noqa: E402

ADMIN = {"X-Admin-Token": "test-admin"}
A01_ORDER = "TA-10431"


def proposal(case_id: str = "CASE-10431") -> dict[str, Any]:
    return {
        "case_id": case_id, "order_id": A01_ORDER, "complaint_category": "missing_items",
        "resolution_type": "partial_refund", "refund_amount": 8.5, "affected_items": ["Carnitas Taco"],
        "policy_rule_ids": ["R1"], "requires_human_approval": False, "customer_message": "Sorry about the tacos.",
        "justification": "Two of three Carnitas Tacos missing; R1.", "policy_version": "2026.09",
        "evidence_refs": [{"tool": "get_order", "record_id": f"order:{A01_ORDER}"}],
        "clarification_or_escalation_reason": "",
    }


def investigation() -> list[AIMessage]:
    return [AIMessage(content="", tool_calls=[tool_call("get_order", {"order_id": A01_ORDER}, "c1")]),
            AIMessage(content="", tool_calls=[tool_call("get_prior_resolutions", {"lookback_days": 90}, "c2")]),
            AIMessage(content="", tool_calls=[tool_call("get_resolution_policy", {"complaint_category": "missing_items"}, "c3")]),
            AIMessage(content="", tool_calls=[tool_call("get_case_status", {}, "c4")]),
            AIMessage(content="Two Carnitas Tacos are missing; R1 applies.")]


def scripted(*case_ids: str) -> ScriptedChatModel:
    responses: list[Any] = []
    for _ in case_ids:
        responses += investigation()
    return ScriptedChatModel(responses=responses, proposals=[proposal(c) for c in case_ids])


@pytest.fixture(scope="session")
def app_db(tmp_path_factory: pytest.TempPathFactory) -> AppDatabase:
    path = tmp_path_factory.mktemp("app") / "taco_app.db"
    write_database(path, fixture_digest())
    return AppDatabase(path)


@pytest.fixture
def case_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TACO_ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("TACO_RESUME_ON_STARTUP", "0")
    module = load_case_api_module(tmp_path / "cases.db")
    with TestClient(module.app) as client:
        yield client


@pytest.fixture
def make_api(tmp_path: Path, app_db: AppDatabase, case_api: TestClient) -> Callable[[ScriptedChatModel], TestClient]:
    """Builds a complaint API around a model; calling it again with the same tmp_path simulates a restart."""
    from complaint_api import main
    from complaint_api.audit import AuditRecorder
    from complaint_api.service import ComplaintService, ServiceSettings
    from cryptography.fernet import Fernet
    from taco_shared.audit import AuditStore

    clients: list[TestClient] = []
    audit_key = Fernet.generate_key()

    def build(model: ScriptedChatModel, quality_rate: float = 0.0, audit: bool = False) -> TestClient:
        for client in clients:
            client.__exit__(None, None, None)
        clients.clear()
        cases = CaseApiClient("http://testserver", transport=InProcessTransport(case_api), retries=2,
                              sleep=lambda _: None)
        deps = WorkflowDeps(model=model, db=app_db, cases=cases, policy=load_policy(), settings=WorkflowSettings())
        main.STATE.clear()
        recorder = AuditRecorder(AuditStore(tmp_path / "audit" / "audit.db", audit_key)) if audit else None
        main.STATE["service"] = ComplaintService(deps, cases, ServiceSettings(
            state_dir=tmp_path / "state", versions={"model": "scripted", "policy_version": "2026.09"},
            quality_db=tmp_path / "quality" / "quality.db", quality_sample_rate=quality_rate, audit=recorder))
        client = TestClient(main.app)
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)
    main.STATE.clear()

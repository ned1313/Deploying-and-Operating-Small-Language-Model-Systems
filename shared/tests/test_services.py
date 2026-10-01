"""Seed, scoped database queries, case API service and client, settings, model factory, and tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from taco_shared.agent.model import build_chat_model
from taco_shared.agent.tools import build_investigation_tools
from taco_shared.case_api_client import CaseApiClient
from taco_shared.config import InferenceSettings
from taco_shared.context import build_context
from taco_shared.dataset import seed
from taco_shared.policy import load_policy

from .conftest import case_transport

A01_ORDER, A01_CUSTOMER, A01_CASE = "TA-10431", "CUST-14159", "CASE-10431"


def test_seed_is_idempotent_and_reset_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    db = tmp_path / "app.db"
    monkeypatch.setattr("sys.argv", ["seed", "--db", str(db)])
    assert seed.main() == 0
    first = db.read_bytes()
    assert seed.main() == 0 and "nothing to do" in capsys.readouterr().out
    assert db.read_bytes() == first
    monkeypatch.setattr(seed, "fixture_digest", lambda: "different")
    assert seed.main() == 2
    monkeypatch.setattr("sys.argv", ["seed", "--db", str(db), "--reset"])
    assert seed.main() == 0


def test_scoped_order_lookup(app_db) -> None:
    assert app_db.get_order(A01_ORDER, A01_CUSTOMER, ("KOP",))["store_id"] == "KOP"
    assert app_db.get_order(A01_ORDER, A01_CUSTOMER, ("WG",)) is None
    assert app_db.get_order(A01_ORDER, "CUST-00000", ("KOP",)) is None


def test_prior_resolutions_window(app_db) -> None:
    rows = app_db.prior_resolutions(A01_CUSTOMER, "2026-04-13", 90)
    assert len(rows) == 1 and rows[0]["resolution_type"] == "store_credit"
    assert app_db.prior_resolutions(A01_CUSTOMER, "2026-04-13", 30) == []


def test_case_api_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    from fastapi.testclient import TestClient

    path = Path(__file__).resolve().parents[1] / "services" / "mock_case_api" / "main.py"
    spec = importlib.util.spec_from_file_location("mock_case_api_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DB_PATH", tmp_path / "cases.db")
    with TestClient(module.app) as client:
        assert client.get("/healthz").json()["cases"] == 980
        assert client.get(f"/cases/{A01_CASE}").status_code == 400
        assert client.get(f"/cases/{A01_CASE}", headers={"X-Store-Scope": "WG"}).status_code == 404
        case = client.get(f"/cases/{A01_CASE}", headers={"X-Store-Scope": "KOP"}).json()
        assert case["order_id"] == A01_ORDER and case["status"] == "open"
        a02 = client.get("/cases", params={"order_id": "TA-10849"}, headers={"X-Store-Scope": "KOP,WG"}).json()
        assert a02[0]["status"] == "resolved" and a02[0]["recorded_actions"][0]["amount"] == 8.95
    with TestClient(module.app) as client:
        assert client.get("/healthz").json()["cases"] == 980


def test_settings_never_expose_the_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "key"
    secret.write_text("sk-test-123456\n", encoding="utf-8")
    monkeypatch.setenv("INFERENCE_API_KEY_FILE", str(secret))
    monkeypatch.setenv("INFERENCE_EXTRA_BODY", '{"chat_template_kwargs": {"enable_thinking": false}}')
    settings = InferenceSettings.from_env()
    assert settings.api_key == "sk-test-123456"
    assert "sk-test" not in repr(settings) and "sk-test" not in json.dumps(settings.describe())
    monkeypatch.setenv("INFERENCE_API_KEY_FILE", str(tmp_path / "missing"))
    assert InferenceSettings.from_env().api_key == "not-needed"


def test_model_factory_request_payload() -> None:
    settings = InferenceSettings(base_url="http://example/v1", model="m", extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    model = build_chat_model(settings)
    payload = model._get_request_payload([HumanMessage("hi")])
    assert payload["seed"] == 42 and payload["temperature"] == 0 and payload["model"] == "m"
    assert payload["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert model.use_responses_api is False


@pytest.fixture
def tools(app_db, case_client):
    ctx = build_context("rep-kop-01", A01_CUSTOMER, A01_CASE, "2026-04-13")
    return {t.name: t for t in build_investigation_tools(ctx, app_db, case_client, load_policy())}


def test_tool_schemas_hide_trusted_context(tools) -> None:
    parameters = {name for t in tools.values()
                  for name in convert_to_openai_tool(t)["function"]["parameters"].get("properties", {})}
    assert parameters == {"order_id", "lookback_days", "complaint_category"}


def test_tools_return_records_and_typed_errors(tools) -> None:
    order = json.loads(tools["get_order"].invoke({"order_id": A01_ORDER}))
    assert order["record_id"] == f"order:{A01_ORDER}" and order["order"]["items"][0]["name"] == "Carnitas Taco"
    assert json.loads(tools["get_order"].invoke({"order_id": "TA-10161"}))["error_kind"] == "not_found"
    assert json.loads(tools["get_order"].invoke({"order_id": "10431"}))["error_kind"] == "invalid_arguments"
    assert json.loads(tools["get_prior_resolutions"].invoke({}))["lookback_days"] == 90
    policy = json.loads(tools["get_resolution_policy"].invoke({"complaint_category": "missing_items"}))
    assert policy["policy_version"] == "2026.09" and policy["rules"][0]["record_id"] == "policy:2026.09#R1"
    case = json.loads(tools["get_case_status"].invoke({}))
    assert case["record_id"] == f"case:{A01_CASE}"


def test_case_api_timeout_is_a_typed_tool_error(app_db, cases_by_id) -> None:
    ctx = build_context("rep-kop-01", A01_CUSTOMER, A01_CASE, "2026-04-13")
    failing = CaseApiClient("http://case-api", transport=case_transport(cases_by_id, fail=True))
    tools = {t.name: t for t in build_investigation_tools(ctx, app_db, failing, load_policy())}
    assert json.loads(tools["get_case_status"].invoke({}))["error_kind"] == "unavailable"

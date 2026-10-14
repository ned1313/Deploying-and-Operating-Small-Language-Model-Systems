"""The mock case-management API: seeding, scoped reads, idempotent actions, faults, and reset."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from taco_shared.testing import load_case_api_module

A01_ORDER, A01_CASE = "TA-10431", "CASE-10431"


def test_case_api_service(tmp_path: Path) -> None:
    module = load_case_api_module(tmp_path / "cases.db")
    with TestClient(module.app) as client:
        assert client.get("/healthz").json()["cases"] == 991
        assert client.get(f"/cases/{A01_CASE}").status_code == 400
        assert client.get(f"/cases/{A01_CASE}", headers={"X-Store-Scope": "WG"}).status_code == 404
        case = client.get(f"/cases/{A01_CASE}", headers={"X-Store-Scope": "KOP"}).json()
        assert case["order_id"] == A01_ORDER and case["status"] == "open"
        a02 = client.get("/cases", params={"order_id": "TA-10849"}, headers={"X-Store-Scope": "KOP,WG"}).json()
        assert a02[0]["status"] == "resolved" and a02[0]["recorded_actions"][0]["amount"] == 8.95
    with TestClient(module.app) as client:
        assert client.get("/healthz").json()["cases"] == 991


def test_case_api_actions_faults_and_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TACO_ADMIN_TOKEN", raising=False)
    module = load_case_api_module(tmp_path / "cases.db")
    scope = {"X-Store-Scope": "KOP"}
    body = {"type": "partial_refund", "amount": 8.5, "proposal_version": 1, "approved_by": "sup-kop-01"}
    with TestClient(module.app) as client:
        assert client.put("/admin/faults", json={"mode": "status"}).status_code == 404
        assert client.post(f"/cases/{A01_CASE}/actions", json=body, headers=scope).status_code == 400
        first = client.post(f"/cases/{A01_CASE}/actions", json=body, headers={**scope, "Idempotency-Key": "k1"})
        assert first.status_code == 201 and first.json()["case"]["status"] == "resolved"
        again = client.post(f"/cases/{A01_CASE}/actions", json=body, headers={**scope, "Idempotency-Key": "k1"})
        assert again.status_code == 200 and again.json()["replayed"] is True
        assert again.json()["action"]["action_id"] == first.json()["action"]["action_id"]
        changed = client.post(f"/cases/{A01_CASE}/actions", json={**body, "amount": 9.0},
                              headers={**scope, "Idempotency-Key": "k1"})
        assert changed.status_code == 409
        other = client.post(f"/cases/{A01_CASE}/actions", json=body, headers={**scope, "Idempotency-Key": "k2"})
        assert other.status_code == 409
        wrong_store = client.post(f"/cases/{A01_CASE}/actions", json=body,
                                  headers={"X-Store-Scope": "WG", "Idempotency-Key": "k3"})
        assert wrong_store.status_code == 404
    monkeypatch.setenv("TACO_ADMIN_TOKEN", "t")
    admin = {"X-Admin-Token": "t"}
    with TestClient(module.app) as client:
        assert client.put("/admin/faults", json={"mode": "status"}).status_code == 403
        client.put("/admin/faults", json={"mode": "status", "status": 503, "count": 1}, headers=admin)
        assert client.get(f"/cases/{A01_CASE}", headers=scope).status_code == 503
        assert client.get(f"/cases/{A01_CASE}", headers=scope).status_code == 200
        assert client.get("/admin/faults", headers=admin).json()["fault"] is None
        assert client.post("/admin/reset", json={"case_ids": [A01_CASE]}, headers=admin).json() == {"reset": 1}
        case = client.get(f"/cases/{A01_CASE}", headers=scope).json()
        assert case["status"] == "open" and case["recorded_actions"] == []

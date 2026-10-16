"""Module 6 additions to the complaint API: audit capture and access, case deletion, and Authelia mode."""

from __future__ import annotations

import sqlite3

import pytest

from .conftest import ADMIN, scripted

REP = {"X-Actor-Id": "rep-kop-01"}
SUP = {"X-Actor-Id": "sup-kop-01"}
AUDITOR = {"X-Actor-Id": "aud-01"}
AREA = {"X-Actor-Id": "sup-area-01"}


def service():
    from complaint_api import main
    return main.STATE["service"]


def kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


def test_audit_is_off_without_a_key(make_api) -> None:
    api = make_api(scripted("CASE-10431"))
    assert api.post("/complaints", json={"scenario_id": "A01"}, headers=REP).status_code == 201
    assert service().audit is None
    assert api.get("/audit/cases/CASE-10431", headers=AUDITOR).status_code == 404


def test_audit_trail_of_an_approved_resolution(make_api) -> None:
    api = make_api(scripted("CASE-10431"), audit=True)
    submitted = api.post("/complaints", json={"scenario_id": "A01"}, headers=REP).json()
    api.post("/reviews/CASE-10431/decision", json={"decision": "approve", "proposal_version": 1}, headers=SUP)

    body = api.get("/audit/cases/CASE-10431", headers=AUDITOR).json()
    events = body["events"]
    assert body["content_included"] is True
    assert kinds(events) == ["intake"] + ["model_call", "tool_access"] * 4 + [
        "model_call", "model_call", "proposal_validated", "workflow_run", "review_decision", "action_recorded",
        "workflow_run"]
    intake = events[0]
    assert intake["actor_id"] == "rep-kop-01" and intake["role"] == "service_rep"
    assert intake["request_id"] == submitted["request_id"]
    assert intake["content"]["intake"]["case_id"] == "CASE-10431" and intake["content"]["intake"]["complaint"]
    tools = [e["metadata"]["tool"] for e in events if e["kind"] == "tool_access"]
    assert tools == ["get_order", "get_prior_resolutions", "get_resolution_policy", "get_case_status"]
    decision = next(e for e in events if e["kind"] == "review_decision")
    validated = next(e for e in events if e["kind"] == "proposal_validated")
    assert decision["actor_id"] == "sup-kop-01" and decision["metadata"]["decision"] == "approve"
    assert decision["metadata"]["proposal_sha256"] == validated["metadata"]["proposal_sha256"]
    action = next(e for e in events if e["kind"] == "action_recorded")
    assert action["metadata"]["idempotency_key"] == "CASE-10431:v1" and action["content"]["action"]["amount"] == 8.5
    assert api.get("/audit/verify", headers=AUDITOR).json()["ok"] is True


def test_audit_readers_and_read_events(make_api) -> None:
    api = make_api(scripted("CASE-10431"), audit=True)
    api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)

    metadata_only = api.get("/audit/cases/CASE-10431", headers=AREA).json()
    assert metadata_only["content_included"] is False
    assert all("content" not in e for e in metadata_only["events"])
    assert api.get("/audit/cases/CASE-10431", headers=REP).status_code == 403
    assert api.get("/audit/cases/CASE-10431", headers={"X-Actor-Id": "sup-wg-01"}).status_code == 403
    assert api.get("/audit/verify", headers=AREA).status_code == 403

    events = service().audit.store.events("CASE-10431")
    reads = [e for e in events if e["kind"] == "audit_read"]
    assert [(e["actor_id"], e["metadata"]["content"]) for e in reads] == [("sup-area-01", False)]
    denied = [e for e in events if e["kind"] == "access_denied"]
    assert {e["actor_id"] for e in denied} == {"rep-kop-01", "sup-wg-01"}


def test_denied_requests_are_audited(make_api) -> None:
    api = make_api(scripted("CASE-10431"), audit=True)
    cross_store = api.post("/complaints", json={"scenario_id": "A01"}, headers={"X-Actor-Id": "rep-wg-01"})
    assert cross_store.status_code == 404
    api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)
    other_store = api.post("/reviews/CASE-10431/decision", json={"decision": "approve", "proposal_version": 1},
                           headers={"X-Actor-Id": "sup-wg-01"})
    quality_reviewer = api.post("/reviews/CASE-10431/decision", json={"decision": "approve", "proposal_version": 1},
                                headers={"X-Actor-Id": "qa-01"})
    assert (other_store.status_code, quality_reviewer.status_code) == (404, 403)

    denied = [e for e in service().audit.store.events() if e["kind"] == "access_denied"]
    assert [(e["actor_id"], e["metadata"]["status"]) for e in denied] == [
        ("rep-wg-01", 404), ("sup-wg-01", 404), ("qa-01", 403)]
    assert denied[0]["metadata"]["route"] == "/complaints" and denied[0]["case_id"] == "CASE-10431"
    assert "content" not in denied[0] and denied[0]["content_id"] is None


def test_case_deletion_keeps_metadata_and_the_chain(make_api) -> None:
    api = make_api(scripted("CASE-10431"), quality_rate=1.0, audit=True)
    api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)
    assert api.delete("/audit/cases/CASE-10431", headers=AREA).status_code == 403

    result = api.delete("/audit/cases/CASE-10431", headers=AUDITOR).json()
    assert result["deleted"]["quality_samples"] == 1 and result["deleted"]["audit_content"] > 0
    assert service().quality.sample("CASE-10431") is None
    view = api.get("/cases/CASE-10431", headers=REP).json()
    assert view["complaint"] is None and view["outcome_reason"] == "content_deleted"

    events = api.get("/audit/cases/CASE-10431", headers=AUDITOR).json()["events"]
    with_content = [e for e in events if e["content_id"]]
    assert with_content and all(e["content"] == {"deleted": True} for e in with_content)
    assert "case_content_deleted" in kinds(events)
    assert api.get("/audit/verify", headers=AUDITOR).json()["ok"] is True


def test_tampering_breaks_the_chain(make_api, tmp_path) -> None:
    api = make_api(scripted("CASE-10431"), audit=True)
    api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)
    with sqlite3.connect(tmp_path / "audit" / "audit.db") as connection:
        connection.execute("UPDATE events SET actor_id = 'someone-else' WHERE seq = 2")
    result = api.get("/audit/verify", headers=AUDITOR).json()
    assert result == {"ok": False, "events": result["events"], "first_bad_seq": 2}


def test_admin_actions_are_audited(make_api) -> None:
    api = make_api(scripted("CASE-10431"), audit=True)
    assert api.post("/admin/reset", json={"case_ids": ["CASE-10431"]}, headers=ADMIN).status_code == 200
    admin = [e for e in service().audit.store.events() if e["kind"] == "admin"]
    assert admin and admin[0]["metadata"]["action"] == "reset_cases"


@pytest.fixture
def authelia_mode(tmp_path, monkeypatch):
    secret = tmp_path / "shared_secret"
    secret.write_text("gateway-secret\n")
    monkeypatch.setenv("APP_AUTH_MODE", "authelia")
    monkeypatch.setenv("APP_GATEWAY_SECRET_FILE", str(secret))
    return {"X-Taco-Gateway": "gateway-secret"}


def as_user(gateway: dict, user: str, groups: str) -> dict:
    return {**gateway, "Remote-User": user, "Remote-Groups": groups}


def test_authelia_mode_needs_the_gateway(make_api, authelia_mode) -> None:
    api = make_api(scripted("CASE-10431"))
    forged = {"Remote-User": "rep-kop-01", "Remote-Groups": "service_rep"}
    assert api.post("/complaints", json={"scenario_id": "A01"}, headers=forged).status_code == 401
    assert api.post("/complaints", json={"scenario_id": "A01"}, headers=REP).status_code == 401
    ok = api.post("/complaints", json={"scenario_id": "A01"}, headers=as_user(authelia_mode, "rep-kop-01", "service_rep"))
    assert ok.status_code == 201 and ok.json()["submitted_by"] == "rep-kop-01"


def test_authelia_mode_roles_come_from_groups(make_api, authelia_mode) -> None:
    api = make_api(scripted("CASE-10431"))
    api.post("/complaints", json={"scenario_id": "A01"}, headers=as_user(authelia_mode, "rep-kop-01", "service_rep"))
    no_role = api.get("/cases", headers=as_user(authelia_mode, "rep-kop-01", "staff"))
    assert no_role.status_code == 403
    # The group decides the role: rep-kop-01 in the reviewer group may not decide on their own submission.
    own = api.post("/reviews/CASE-10431/decision", json={"decision": "approve", "proposal_version": 1},
                   headers=as_user(authelia_mode, "rep-kop-01", "reviewer"))
    assert own.status_code == 403
    approved = api.post("/reviews/CASE-10431/decision", json={"decision": "approve", "proposal_version": 1},
                        headers=as_user(authelia_mode, "sup-kop-01", "reviewer"))
    assert approved.status_code == 200 and approved.json()["status"] == "recorded"


def test_authelia_mode_admin_group(make_api, authelia_mode) -> None:
    api = make_api(scripted("CASE-10431"))
    reset = {"json": {"case_ids": ["CASE-10431"]}}
    assert api.post("/admin/reset", headers=ADMIN, **reset).status_code == 401
    assert api.post("/admin/reset", headers=as_user(authelia_mode, "sup-area-01", "area_manager"), **reset).status_code == 403
    assert api.post("/admin/reset", headers=as_user(authelia_mode, "admin-01", "admin"), **reset).status_code == 200


def test_authelia_mode_ui_uses_the_signed_in_user(make_api, authelia_mode) -> None:
    api = make_api(scripted("CASE-10431"))
    page = api.get("/ui?actor=sup-area-01", headers=as_user(authelia_mode, "sup-kop-01", "reviewer")).text
    assert "Signed in through Authelia as <b>sup-kop-01" in page and "Act as:" not in page


def test_audit_pages(make_api) -> None:
    api = make_api(scripted("CASE-10431"), audit=True)
    api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)

    recent = api.get("/ui/audit?actor=aud-01").text
    assert "Hash chain verified" in recent and "tool_access" in recent and "/ui/audit/CASE-10431" in recent
    trail = api.get("/ui/audit/CASE-10431?actor=aud-01").text
    assert "decrypted for you" in trail and "Delete content" in trail and "get_order" in trail
    area = api.get("/ui/audit/CASE-10431?actor=sup-area-01").text
    assert "Metadata only for your role" in area and "Delete content" not in area and "decrypted" not in area
    assert api.get("/ui/audit?actor=rep-kop-01").status_code == 403
    assert api.get("/ui/audit?actor=aud-01&case=CASE-10431", follow_redirects=False).status_code == 303
    assert "Audit trail for this case" in api.get("/ui/cases/CASE-10431?actor=aud-01").text

    deleted = api.post("/ui/audit/CASE-10431/delete", data={"actor": "aud-01"}, follow_redirects=False)
    assert deleted.status_code == 303 and "Content%20deleted" in deleted.headers["location"]
    refused = api.post("/ui/audit/CASE-10431/delete", data={"actor": "sup-area-01"}, follow_redirects=False)
    assert "only%20auditors" in refused.headers["location"]
    reads = [e for e in service().audit.store.events() if e["kind"] == "audit_read"]
    assert {e["actor_id"] for e in reads} == {"aud-01", "sup-area-01"}

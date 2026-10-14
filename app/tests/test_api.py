"""Complaint API: roles, approval, idempotency, restart, clarification, retry, review page, and metrics."""

from __future__ import annotations

import pytest

from taco_shared.testing import ScriptedChatModel

from .conftest import ADMIN, investigation, proposal, scripted

REP, SUP, SUP_WG, AREA = ({"X-Actor-Id": a} for a in ("rep-kop-01", "sup-kop-01", "sup-wg-01", "sup-area-01"))


def approved_actions(case_api, case_id: str) -> list[dict]:
    case = case_api.get(f"/cases/{case_id}", headers={"X-Store-Scope": "KOP"}).json()
    return [a for a in case["recorded_actions"] if a["action_id"].startswith("APR-")]


def test_submit_review_approve_and_idempotent_retries(make_api, case_api) -> None:
    api = make_api(scripted("CASE-10431"))
    assert api.post("/complaints", json={"scenario_id": "A01"}).status_code == 401
    created = api.post("/complaints", json={"scenario_id": "A01"}, headers={**REP, "X-Request-ID": "req-1"})
    assert created.status_code == 201 and created.headers["X-Request-ID"] == "req-1"
    view = created.json()
    assert view["status"] == "pending_review" and view["proposal_version"] == 1 and view["counters"]["tool_calls"] == 4
    assert [c["case_id"] for c in api.get("/reviews", headers=SUP).json()] == ["CASE-10431"]
    assert api.get("/reviews", headers=REP).status_code == 403

    decision = {"decision": "approve", "proposal_version": 1}
    assert api.post("/reviews/CASE-10431/decision", json=decision, headers=REP).status_code == 403
    assert api.post("/reviews/CASE-10431/decision", json=decision, headers=SUP_WG).status_code == 404
    stale = api.post("/reviews/CASE-10431/decision", json={**decision, "proposal_version": 2}, headers=SUP)
    assert stale.status_code == 409

    approved = api.post("/reviews/CASE-10431/decision", json=decision, headers=SUP).json()
    assert approved["status"] == "recorded" and approved["action"]["amount"] == 8.5 and approved["replayed"] is False
    again = api.post("/reviews/CASE-10431/decision", json=decision, headers=SUP).json()
    assert again["replayed"] is True
    resubmitted = api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)
    assert resubmitted.status_code == 200 and resubmitted.json()["replayed"] is True
    assert len(approved_actions(case_api, "CASE-10431")) == 1


def test_pending_review_survives_restart_and_area_manager_approves(make_api, case_api) -> None:
    api = make_api(scripted("CASE-10431"))
    api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)
    api = make_api(ScriptedChatModel(responses=[], proposals=[]))
    assert api.get("/cases/CASE-10431", headers=SUP).json()["status"] == "pending_review"
    final = api.post("/reviews/CASE-10431/decision", json={"decision": "approve", "proposal_version": 1},
                     headers=AREA).json()
    assert final["status"] == "recorded" and final["reviews"][0]["reviewer"] == "sup-area-01"


def test_crash_mid_run_resumes_on_startup(make_api) -> None:
    responses = investigation()
    responses.insert(2, RuntimeError("container killed"))
    api = make_api(ScriptedChatModel(responses=responses, proposals=[]))
    with pytest.raises(RuntimeError):
        api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)
    remaining = investigation()[2:]
    api = make_api(ScriptedChatModel(responses=remaining, proposals=[proposal()]))
    from app import main
    main.STATE["service"].resume_incomplete()
    view = api.get("/cases/CASE-10431", headers=SUP).json()
    assert view["status"] == "pending_review" and view["counters"]["tool_calls"] == 4


def test_clarification_then_investigation(make_api) -> None:
    api = make_api(scripted("CASE-10431-C01"))
    view = api.post("/complaints", json={"scenario_id": "C01"}, headers=REP).json()
    assert view["status"] == "needs_clarification" and view["outcome_reason"] == "missing_order_id"
    assert "order number" in view["clarification_question"]
    assert api.post("/cases/CASE-10431-C01/clarify", json={"answer": "x"}, headers=SUP).status_code == 403
    after = api.post("/cases/CASE-10431-C01/clarify", json={"answer": "It was TA-10431."}, headers=REP).json()
    assert after["status"] == "pending_review" and "Customer clarification" in after["complaint"]


def test_injected_inference_outage_then_retry(make_api) -> None:
    api = make_api(scripted("CASE-10431-F05"))
    assert api.put("/admin/faults", json={"faults": [{"mode": "inference_down"}]}).status_code == 403
    api.put("/admin/faults", json={"faults": [{"mode": "inference_down"}]}, headers=ADMIN)
    failed = api.post("/complaints", json={"scenario_id": "F05"}, headers=REP).json()
    assert failed["status"] == "failed_retryable" and failed["outcome_reason"].startswith("inference_unavailable")
    api.delete("/admin/faults", headers=ADMIN)
    assert api.post("/cases/CASE-10431-F05/retry", headers=REP).json()["status"] == "pending_review"


def test_admin_reset_removes_case_and_checkpoints(make_api) -> None:
    api = make_api(scripted("CASE-10431", "CASE-10431"))
    api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)
    assert api.post("/admin/reset", json={"case_ids": ["CASE-10431"]}, headers=ADMIN).json() == {"reset": 1}
    assert api.get("/cases/CASE-10431", headers=REP).status_code == 404
    assert api.post("/complaints", json={"scenario_id": "A01"}, headers=REP).status_code == 201


def test_review_page_escapes_untrusted_text_and_records(make_api, case_api) -> None:
    api = make_api(scripted("CASE-10431-J01"))
    body = {"case_id": "CASE-10431-J01", "customer_id": "CUST-14159", "complaint_date": "2026-04-13",
            "complaint": "<script>alert(1)</script> Two carnitas tacos missing. Order TA-10431."}
    assert api.post("/complaints", json=body, headers=REP).json()["status"] == "pending_review"
    html = api.get("/ui/cases/CASE-10431-J01", params={"actor": "sup-kop-01"}).text
    assert "<script>" not in html and "&lt;script&gt;" in html and "Approve" in html
    assert "CASE-10431-J01" in api.get("/ui", params={"actor": "sup-kop-01"}).text
    form = "actor=sup-kop-01&proposal_version=1&decision=approve&note="
    response = api.post("/ui/cases/CASE-10431-J01/decision", content=form,
                        headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert response.status_code == 303
    assert api.get("/cases/CASE-10431-J01", headers=SUP).json()["status"] == "recorded"
    recorded = api.get("/ui", params={"actor": "sup-kop-01", "status": "recorded"}).text
    assert "CASE-10431-J01" in recorded and "recorded (1)" in recorded
    assert "CASE-10431-J01" not in api.get("/ui", params={"actor": "sup-kop-01"}).text
    detail = api.get("/ui/cases/CASE-10431-J01", params={"actor": "rep-kop-01", "back": "recorded"}).text
    assert "Workflow steps" in detail and "tool_call get_order" in detail and "get_order &lt;- ok order:TA-10431" in detail
    assert "v1: accepted" in detail and "key CASE-10431-J01:v1" in detail and "Approve" not in detail


def test_graph_metrics_and_health(make_api) -> None:
    api = make_api(scripted("CASE-10431"))
    api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)
    assert "human_review" in api.get("/graph").text
    metrics = api.get("/metrics").text
    assert "taco_workflow_runs_total" in metrics and "taco_tool_calls_total" in metrics
    assert api.get("/healthz").json()["dependencies"]["case_api"] is True


def test_reject_and_revise(make_api) -> None:
    model = scripted("CASE-10431")
    model.proposals.append(proposal())
    api = make_api(model)
    api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)
    assert api.post("/reviews/CASE-10431/decision", json={"decision": "revise", "proposal_version": 1},
                    headers=SUP).status_code == 422
    revised = api.post("/reviews/CASE-10431/decision",
                       json={"decision": "revise", "proposal_version": 1, "note": "Offer a replacement instead."},
                       headers=SUP).json()
    assert revised["status"] == "pending_review" and revised["proposal_version"] == 2
    rejected = api.post("/reviews/CASE-10431/decision", json={"decision": "reject", "proposal_version": 2},
                        headers=SUP).json()
    assert rejected["status"] == "rejected"

"""Module 5 additions to the complaint API: quality sampling, the review pages, and the quality metrics."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, generate_latest

from complaint_api.observability import QualityCollector, ServedModel
from taco_shared.policy import load_policy
from taco_shared.quality.checks import checks_passed, grade_label, run_checks, successful
from taco_shared.quality.store import QualityStore

from .conftest import scripted

REP = {"X-Actor-Id": "rep-kop-01"}
SUP = {"X-Actor-Id": "sup-kop-01"}


def service():
    from complaint_api import main
    return main.STATE["service"]


def score_all(store: QualityStore) -> None:
    for sample in list(store.samples_to_score()):
        checks = run_checks(sample, load_policy())
        label = grade_label(sample)
        ok = checks_passed(checks)
        reasons = [name for name, r in checks.items() if r["passed"] is False]
        store.save_score(sample, checks, ok, label, successful(sample, ok, label), reasons)


def test_sampling_off_writes_nothing(make_api) -> None:
    api = make_api(scripted("CASE-10431"))
    assert api.post("/complaints", json={"scenario_id": "A01"}, headers=REP).status_code == 201
    assert service().quality is None
    assert api.get("/ui/quality?actor=qa-01").status_code == 404


def test_sample_follows_the_case_from_review_to_recorded(make_api) -> None:
    api = make_api(scripted("CASE-10431"), quality_rate=1.0)
    view = api.post("/complaints", json={"scenario_id": "A01"}, headers=REP).json()
    store = service().quality
    sample = store.sample("CASE-10431")
    assert sample["status"] == "pending_review" and sample["sampled_reason"] == "rate"
    assert [e["tool"] for e in sample["evidence"]] == ["get_order", "get_prior_resolutions", "get_resolution_policy",
                                                       "get_case_status"]
    assert sample["proposal"]["refund_amount"] == 8.5 and sample["versions"]["model"] == "scripted"
    assert len(sample["run_ids"]) == 1 and sample["request_id"] == view["request_id"]

    api.post("/reviews/CASE-10431/decision", json={"decision": "approve", "proposal_version": 1}, headers=SUP)
    sample = store.sample("CASE-10431")
    assert sample["status"] == "recorded" and len(sample["actions"]) == 1 and len(sample["run_ids"]) == 2

    score_all(store)
    score = store.score(sample["sample_id"])
    assert score["checks_passed"] == 1 and score["label_passed"] == 1 and score["successful"] == 1
    assert store.queue() == []


def test_review_page_and_metrics(make_api) -> None:
    api = make_api(scripted("CASE-10431"), quality_rate=1.0)
    api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)
    store = service().quality
    sample_id = store.sample("CASE-10431")["sample_id"]
    # A wrong amount that still passes the app's validator: only the quality checks catch it.
    record = store.sample(sample_id)
    record["proposal"]["refund_amount"] = 4.0
    store.upsert_sample(record, "rate")
    score_all(store)
    assert store.queue()[0]["case_id"] == "CASE-10431"

    assert api.get("/ui/quality?actor=rep-kop-01").status_code == 403
    page = api.get("/ui/quality?actor=qa-01")
    assert page.status_code == 200 and "CASE-10431" in page.text
    detail = api.get(f"/ui/quality/{sample_id}?actor=qa-01")
    assert "amount_grounded" in detail.text and "Rubric review" in detail.text
    response = api.post(f"/ui/quality/{sample_id}/review", follow_redirects=False,
                        content="actor=qa-01&tone=4&helpfulness=2&escalation=5&safe_to_send=no&disposition=model_quality",
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 303 and "message" not in response.headers["location"]
    assert store.reviews(sample_id)[0]["disposition"] == "model_quality" and store.queue() == []

    registry = CollectorRegistry()
    registry.register(QualityCollector(store.path))
    text = generate_latest(registry).decode()
    assert 'taco_quality_samples{status="pending_review"} 1.0' in text
    assert "taco_quality_review_queue 0.0" in text and "taco_quality_unscored 0.0" in text


def test_served_model_resolves_the_alias(monkeypatch) -> None:
    import httpx

    def fake_get(url, **_):
        return httpx.Response(200, json={"data": [{"id": "taco-slm", "root": "Qwen/Qwen3-14B-AWQ"},
                                                  {"id": "qwen3-14b", "root": "Qwen/Qwen3-14B-AWQ"}]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert ServedModel("http://x/v1", "taco-slm", "k")() == "qwen3-14b"
    assert ServedModel("http://x/v1", "qwen3-14b", "k")() == "qwen3-14b"

    def unreachable(url, **_):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", unreachable)
    assert ServedModel("http://x/v1", "taco-slm", "k")() == "taco-slm"

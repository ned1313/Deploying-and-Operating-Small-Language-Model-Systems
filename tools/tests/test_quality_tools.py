"""Module 5 tools: quality scoring and report, release gates, and cost per successful resolution."""

from __future__ import annotations

import json
from pathlib import Path

from taco_shared.quality.store import QualityStore
from taco_tools.quality import cost_report, quality, release_report

from .conftest import scripted

REP = {"X-Actor-Id": "rep-kop-01"}
SUP = {"X-Actor-Id": "sup-kop-01"}


def test_score_report_and_review(make_api, tmp_path: Path, monkeypatch, capsys) -> None:
    from complaint_api import main

    api = make_api(scripted("CASE-10431"), quality_rate=1.0)
    api.post("/complaints", json={"scenario_id": "A01"}, headers=REP)
    api.post("/reviews/CASE-10431/decision", json={"decision": "approve", "proposal_version": 1}, headers=SUP)
    store: QualityStore = main.STATE["service"].quality
    monkeypatch.setattr(quality, "RESULTS", tmp_path / "quality")

    scored = quality.score_pending(store)
    assert len(scored) == 1 and scored[0]["successful"] is True and scored[0]["flag_reasons"] == []
    payload = quality.report(store, "t", None, None)
    summary = payload["summary"]["scripted"]
    assert summary["checks_pass_rate"] == 1.0 and summary["labeled_pass_rate"] == 1.0 and not summary["degraded"]
    assert (tmp_path / "quality").glob("quality-t-*.md")

    sample_id = store.sample("CASE-10431")["sample_id"]
    store.add_review(sample_id, "qa-01", 2, 4, 4, True, "model_quality", "too terse")
    rescored = quality.score_sample(store, store.sample(sample_id), quality.load_gates())
    assert any(reason.startswith("rubric:") for reason in rescored["flag_reasons"])


def test_degradation_needs_enough_samples() -> None:
    gates = quality.load_gates()
    row = {"model": "m", "checks": {}, "checks_passed": 0, "label_passed": 0, "successful": 0, "escalated": 0,
           "flagged": 1}
    assert not quality.summarize([row] * 4, gates)["m"]["degraded"]
    verdict = quality.summarize([row] * 6, gates)["m"]
    assert verdict["degraded"] and len(verdict["degraded_reasons"]) == 2


def release_evidence(passed: float, policy: float, p95: float, vram: float, failures: int = 0) -> dict:
    return {"release_id": "r", "files": {}, "e2e": {"rates": {"passed": passed, "policy_compliant": policy, "grounded": 1.0,
                                                          "schema_valid": 1.0, "duplicate_free": 1.0}},
            "failure": {"total": 17, "passed": 17 - failures, "failed": []},
            "load": {"workflow_p95_s": p95, "peak_vram_gib": vram, "failed_requests": 0, "failed_retryable": 0}}


def test_release_gates() -> None:
    gates = quality.load_gates()
    baseline = release_evidence(0.88, 0.88, 14.3, 21.6)
    same = release_report.evaluate(baseline, release_evidence(0.88, 0.88, 15.0, 21.0), gates)
    assert all(g["result"] == "pass" for g in same)
    worse = release_report.evaluate(baseline, release_evidence(0.71, 0.71, 9.0, 18.0, failures=1), gates)
    failed = {g["gate"] for g in worse if g["result"] == "FAIL"}
    assert failed == {"e2e labeled pass rate", "policy compliant", "failure tests"}


def test_release_report_reads_labeled_results(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(release_report, "RESULTS_ROOT", tmp_path)
    monkeypatch.setattr(release_report, "RELEASES", tmp_path / "releases")
    (tmp_path / "e2e").mkdir()
    (tmp_path / "e2e" / "e2e-qwen3-14b-r1-20261014-120000.json").write_text(json.dumps(
        {"model": "qwen3-14b", "rates": {"passed": 0.88}, "outcomes": {}, "rows": [
            {"scenario_id": "S09", "grades": {"passed": False}}]}))
    data = release_report.evidence("r1")
    assert data["e2e"]["failed"] == ["S09"] and data["files"]["failure"] is None


def test_cost_per_successful_resolution() -> None:
    a = cost_report.assumptions()
    meta = {"started_epoch": 0, "ended_epoch": 1800}
    result = cost_report.cost(meta, {"gpu_power_w": 356, "input_tokens": 300000, "output_tokens": 20000},
                              {"successful": 40, "recorded": 45}, a)
    hourly = a["gpu_host_price_usd"] / (a["amortization_years"] * 8760) \
        + (356 + a["host_overhead_watts"]) / 1000 * a["electricity_usd_per_kwh"] + a["desktop_share_usd_per_hour"]
    assert result["successful_per_hour"] == 80.0
    assert result["on_prem_usd_per_success"] == round(hourly * 0.5 / 40, 4)
    assert result["tokens_per_success"] == 8000
    assert cost_report.cost(meta, {}, {"successful": 0, "recorded": 0}, a)["on_prem_usd_per_success"] is None

"""The e2e and failure harnesses against the in-process complaint API with a scripted model."""

from __future__ import annotations

from taco_shared.api_client import Api
from taco_shared.paths import AGENT_SCENARIOS_V2, GENERATED_DIR, load_json
from taco_shared.testing import InProcessTransport
from taco_tools.e2e import failure_tests, run_e2e_scenarios

from .conftest import scripted


def scenario(scenario_id: str) -> dict:
    return next(s for s in load_json(AGENT_SCENARIOS_V2)["scenarios"] if s["scenario_id"] == scenario_id)


def harness_api(api, case_api, monkeypatch) -> Api:
    monkeypatch.setenv("TACO_ADMIN_TOKEN", "test-admin")
    return Api("http://testserver", "http://testserver", InProcessTransport(api), InProcessTransport(case_api))


def test_failure_harness_rows(make_api, case_api, monkeypatch) -> None:
    model = scripted("CASE-10431-C01", "CASE-10431-F03", "CASE-10431")
    model.proposals.insert(1, dict(model.proposals[1]))  # F03 needs a second proposal after the injected fault
    api = make_api(model)
    client = harness_api(api, case_api, monkeypatch)
    c01 = failure_tests.run_workflow_scenario(client, scenario("C01"))
    assert c01["passed"], c01
    f03 = failure_tests.run_workflow_scenario(client, scenario("F03"))
    assert f03["passed"] and f03["counters"]["corrections"] == 1, f03
    rows = failure_tests.approval_checks(client)
    assert [r["id"] for r in rows] == ["D01", "D02", "D03", "D04", "D05", "D06"]
    assert all(r["passed"] for r in rows), rows


def test_e2e_runner_records_once(make_api, case_api, monkeypatch) -> None:
    api = make_api(scripted("CASE-10431"))
    client = harness_api(api, case_api, monkeypatch)
    orders = {o["order_id"]: o for o in load_json(GENERATED_DIR / "orders.json")}
    row = run_e2e_scenarios.run_scenario(client, scenario("A01"), orders)
    assert row["status"] == "recorded" and row["actions"] == 1 and row["duplicate_free"]
    assert row["grades"]["passed"]

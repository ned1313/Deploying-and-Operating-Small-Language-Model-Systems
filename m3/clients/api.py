"""Small HTTP helpers for the complaint API and case API, shared by the CLI and the harnesses."""

from __future__ import annotations

import os
from typing import Any

import httpx

COMPLAINT_API_URL = os.environ.get("COMPLAINT_API_URL", "http://complaint-api:8090")
CASE_API_URL = os.environ.get("CASE_API_URL", "http://case-api:8081")
# Runs can take tens of seconds (model calls, injected timeouts); keep the client patient.
TIMEOUT = httpx.Timeout(float(os.environ.get("CLIENT_TIMEOUT_SECONDS") or 180), connect=5.0)


class Api:
    def __init__(self, base_url: str = COMPLAINT_API_URL, case_url: str = CASE_API_URL,
                 transport: httpx.BaseTransport | None = None, case_transport: httpx.BaseTransport | None = None) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=TIMEOUT, transport=transport)
        self.case_http = httpx.Client(base_url=case_url, timeout=TIMEOUT, transport=case_transport)
        self.admin = {"X-Admin-Token": os.environ.get("TACO_ADMIN_TOKEN", "")}

    def call(self, method: str, path: str, actor: str | None = None, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if actor:
            headers["X-Actor-Id"] = actor
        return self.http.request(method, path, headers=headers, **kwargs)

    def submit(self, actor: str, scenario_id: str) -> httpx.Response:
        return self.call("POST", "/complaints", actor, json={"scenario_id": scenario_id})

    def decide(self, actor: str, case_id: str, decision: str, version: int, note: str | None = None) -> httpx.Response:
        return self.call("POST", f"/reviews/{case_id}/decision", actor,
                         json={"decision": decision, "proposal_version": version, "note": note})

    def case(self, actor: str, case_id: str, steps: bool = False) -> httpx.Response:
        return self.call("GET", f"/cases/{case_id}", actor, params={"steps": steps})

    def approved_actions(self, case_id: str, store_id: str) -> list[dict[str, Any]]:
        response = self.case_http.get(f"/cases/{case_id}", headers={"X-Store-Scope": store_id})
        response.raise_for_status()
        return [a for a in response.json()["recorded_actions"] if a["action_id"].startswith("APR-")]

    def reset(self, case_ids: list[str]) -> None:
        for client in (self.http, self.case_http):
            client.post("/admin/reset", json={"case_ids": case_ids}, headers=self.admin).raise_for_status()

    def set_model_faults(self, faults: list[dict[str, Any]]) -> None:
        self.http.put("/admin/faults", json={"faults": faults}, headers=self.admin).raise_for_status()

    def set_case_api_fault(self, fault: dict[str, Any]) -> None:
        self.case_http.put("/admin/faults", json=fault, headers=self.admin).raise_for_status()

    def clear_faults(self) -> None:
        self.http.delete("/admin/faults", headers=self.admin).raise_for_status()
        self.case_http.delete("/admin/faults", headers=self.admin).raise_for_status()

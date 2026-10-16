"""Small HTTP helpers for the complaint API and case API, shared by the CLI and the harnesses.

APP_AUTH_MODE=header (modules 2-5): calls go straight to the APIs with `X-Actor-Id`, and admin routes use
TACO_ADMIN_TOKEN. APP_AUTH_MODE=authelia (module 6): calls go through app-gateway over HTTPS with HTTP Basic
(the actor id and the shared demo password); admin routes use the synthetic admin user, and the case API's
admin routes are reached through the gateway's /case-admin/ prefix.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

COMPLAINT_API_URL = os.environ.get("COMPLAINT_API_URL", "http://complaint-api:8090")
CASE_API_URL = os.environ.get("CASE_API_URL", "http://case-api:8081")
APP_GATEWAY_URL = os.environ.get("APP_GATEWAY_URL", "https://app.taco.internal:8443")
# Runs can take tens of seconds (model calls, injected timeouts); keep the client patient.
TIMEOUT = httpx.Timeout(float(os.environ.get("CLIENT_TIMEOUT_SECONDS") or 180), connect=5.0)


def authelia_mode() -> bool:
    return (os.environ.get("APP_AUTH_MODE") or "header").lower() == "authelia"


def user_password() -> str:
    path = os.environ.get("TACO_USER_PASSWORD_FILE") or "/run/taco-users/password"
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        raise SystemExit(f"APP_AUTH_MODE=authelia needs the demo user password in {path}") from None


class Api:
    def __init__(self, base_url: str = COMPLAINT_API_URL, case_url: str = CASE_API_URL,
                 transport: httpx.BaseTransport | None = None, case_transport: httpx.BaseTransport | None = None,
                 admin_user: str | None = None) -> None:
        self.gateway = authelia_mode()
        if self.gateway:
            verify = os.environ.get("APP_CA_FILE") or True
            self.password = user_password()
            self.admin_user = admin_user or os.environ.get("TACO_ADMIN_USER") or "admin-01"
            self.http = httpx.Client(base_url=APP_GATEWAY_URL, timeout=TIMEOUT, transport=transport, verify=verify)
        else:
            self.http = httpx.Client(base_url=base_url, timeout=TIMEOUT, transport=transport)
        self.case_http = httpx.Client(base_url=case_url, timeout=TIMEOUT, transport=case_transport)
        self.admin = {"X-Admin-Token": os.environ.get("TACO_ADMIN_TOKEN", "")}

    def call(self, method: str, path: str, actor: str | None = None, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if actor and self.gateway:
            kwargs["auth"] = (actor, self.password)
        elif actor:
            headers["X-Actor-Id"] = actor
        return self.http.request(method, path, headers=headers, **kwargs)

    def _admin(self, method: str, service: str, path: str, **kwargs: Any) -> httpx.Response:
        """Admin call to the complaint API (service="app") or the case API (service="case")."""
        if self.gateway:
            prefix = "/case-admin" if service == "case" else "/admin"
            response = self.http.request(method, prefix + path, auth=(self.admin_user, self.password), **kwargs)
        else:
            client = self.case_http if service == "case" else self.http
            response = client.request(method, "/admin" + path, headers=self.admin, **kwargs)
        response.raise_for_status()
        return response

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
        for service in ("app", "case"):
            self._admin("POST", service, "/reset", json={"case_ids": case_ids})

    def set_model_faults(self, faults: list[dict[str, Any]]) -> None:
        self._admin("PUT", "app", "/faults", json={"faults": faults})

    def set_case_api_fault(self, fault: dict[str, Any]) -> None:
        self._admin("PUT", "case", "/faults", json=fault)

    def clear_faults(self) -> None:
        self._admin("DELETE", "app", "/faults")
        self._admin("DELETE", "case", "/faults")

"""Client for the mock case-management API."""

from __future__ import annotations

import os
import time
from typing import Any, Callable

import httpx

DEFAULT_CASE_API_URL = os.environ.get("CASE_API_URL", "http://case-api:8081")


class CaseApiUnavailable(RuntimeError):
    """Transport failure or server error from the case API, after any retries."""


class CaseApiConflict(RuntimeError):
    """The case API refused an action because it conflicts with one already recorded."""

    def __init__(self, detail: Any) -> None:
        super().__init__(str(detail))
        self.detail = detail


class CaseApiClient:
    """Module 2 uses no retries; module 3 passes a retry budget (`retries`, `backoff_seconds`)."""

    def __init__(self, base_url: str = DEFAULT_CASE_API_URL, timeout: httpx.Timeout | None = None,
                 transport: httpx.BaseTransport | None = None, retries: int = 0, backoff_seconds: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"),
                                    timeout=timeout or httpx.Timeout(5.0, connect=3.0), transport=transport)
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep
        self.retries_used = 0

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = self._client.request(method, path, **kwargs)
                failure = f"case API error: HTTP {response.status_code}" if response.status_code >= 500 else None
            except httpx.HTTPError as error:
                response, failure = None, f"case API unreachable: {type(error).__name__}"
            if failure is None:
                return response  # type: ignore[return-value]
            if attempt >= self.retries:
                raise CaseApiUnavailable(failure + (f" after {attempt} retries" if attempt else ""))
            self._sleep(self.backoff_seconds * (2 ** attempt))
            attempt += 1
            self.retries_used += 1

    def get_case(self, case_id: str, store_ids: tuple[str, ...]) -> dict[str, Any] | None:
        """Return the case, or None when it does not exist or is outside the store scope."""
        response = self._request("GET", f"/cases/{case_id}", headers={"X-Store-Scope": ",".join(store_ids)})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def record_action(self, case_id: str, store_ids: tuple[str, ...], action: dict[str, Any],
                      idempotency_key: str) -> dict[str, Any]:
        """Record an approved action. Retries reuse the same idempotency key, so they never duplicate."""
        response = self._request("POST", f"/cases/{case_id}/actions", json=action,
                                 headers={"X-Store-Scope": ",".join(store_ids), "Idempotency-Key": idempotency_key})
        if response.status_code == 409:
            raise CaseApiConflict(response.json().get("detail"))
        response.raise_for_status()
        return response.json()

    def health(self) -> bool:
        try:
            return self._client.get("/healthz").status_code == 200
        except httpx.HTTPError:
            return False

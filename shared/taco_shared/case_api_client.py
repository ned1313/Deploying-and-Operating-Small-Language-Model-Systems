"""Client for the mock case-management API."""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_CASE_API_URL = os.environ.get("CASE_API_URL", "http://case-api:8081")


class CaseApiUnavailable(RuntimeError):
    """Transport failure or server error from the case API."""


class CaseApiClient:
    def __init__(self, base_url: str = DEFAULT_CASE_API_URL, timeout: httpx.Timeout | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"),
                                    timeout=timeout or httpx.Timeout(5.0, connect=3.0), transport=transport)

    def get_case(self, case_id: str, store_ids: tuple[str, ...]) -> dict[str, Any] | None:
        """Return the case, or None when it does not exist or is outside the store scope."""
        try:
            response = self._client.get(f"/cases/{case_id}", headers={"X-Store-Scope": ",".join(store_ids)})
        except httpx.HTTPError as error:
            raise CaseApiUnavailable(f"case API unreachable: {type(error).__name__}") from error
        if response.status_code == 404:
            return None
        if response.status_code >= 500:
            raise CaseApiUnavailable(f"case API error: HTTP {response.status_code}")
        response.raise_for_status()
        return response.json()

    def health(self) -> bool:
        try:
            return self._client.get("/healthz").status_code == 200
        except httpx.HTTPError:
            return False

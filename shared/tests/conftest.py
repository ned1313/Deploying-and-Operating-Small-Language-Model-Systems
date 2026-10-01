"""Shared test helpers: a seeded temporary database and an in-process case API transport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from taco_shared.case_api_client import CaseApiClient
from taco_shared.dataset.seed import fixture_digest, write_database
from taco_shared.db import AppDatabase
from taco_shared.paths import GENERATED_DIR, load_json

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def app_db(tmp_path_factory: pytest.TempPathFactory) -> AppDatabase:
    path = tmp_path_factory.mktemp("app") / "taco_app.db"
    write_database(path, fixture_digest())
    return AppDatabase(path)


@pytest.fixture(scope="session")
def cases_by_id() -> dict[str, dict[str, Any]]:
    return {case["case_id"]: case for case in load_json(GENERATED_DIR / "cases.json")}


def case_transport(cases: dict[str, dict[str, Any]], fail: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectTimeout("simulated timeout", request=request)
        scope = set(request.headers.get("X-Store-Scope", "").split(","))
        case = cases.get(request.url.path.rsplit("/", 1)[-1])
        if case is None or case["store_id"] not in scope:
            return httpx.Response(404, json={"detail": "case not found"})
        return httpx.Response(200, content=json.dumps({k: v for k, v in case.items()}))
    return httpx.MockTransport(handler)


@pytest.fixture
def case_client(cases_by_id: dict[str, dict[str, Any]]) -> CaseApiClient:
    return CaseApiClient("http://case-api", transport=case_transport(cases_by_id))

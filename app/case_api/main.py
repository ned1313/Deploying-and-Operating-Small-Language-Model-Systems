"""Mock case-management API. Owns its own SQLite file; seeds it from the generated cases on start.

Run: uvicorn main:app --host 0.0.0.0 --port 8081

Module 3 adds idempotent approved actions (POST /cases/{case_id}/actions) and admin routes for fault
injection and fixture reset. Admin routes exist only when TACO_ADMIN_TOKEN is set.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import random
import sqlite3
import threading
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from taco_shared.paths import GENERATED_DIR, load_json

DB_PATH = Path(os.environ.get("TACO_CASE_DB", "/data/cases/cases.db"))
ACTION_TYPES = ("store_credit", "partial_refund", "full_refund", "replacement", "apology_only",
                "escalate_to_manager", "no_action")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (case_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, customer_id TEXT NOT NULL,
                                  store_id TEXT NOT NULL, status TEXT NOT NULL, opened_at TEXT NOT NULL, notes TEXT);
CREATE TABLE IF NOT EXISTS case_actions (action_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(case_id),
                                         type TEXT NOT NULL, amount REAL NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS action_keys (idempotency_key TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                                        request_hash TEXT NOT NULL, action_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cases_order ON cases (order_id);
"""


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def seed_missing() -> int:
    """Insert generated cases that are not in the database yet; never touches recorded data."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    cases = load_json(GENERATED_DIR / "cases.json")
    with closing(connect()) as connection, connection:
        connection.executescript(SCHEMA)
        before = connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
        connection.executemany("INSERT OR IGNORE INTO cases VALUES (?, ?, ?, ?, ?, ?, ?)", [
            (c["case_id"], c["order_id"], c["customer_id"], c["store_id"], c["status"], c["opened_at"], c["notes"])
            for c in cases])
        connection.executemany("INSERT OR IGNORE INTO case_actions VALUES (?, ?, ?, ?, ?)", [
            (a["action_id"], c["case_id"], a["type"], a["amount"], a["recorded_at"])
            for c in cases for a in c["recorded_actions"]])
        return connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] - before


class FaultConfig(BaseModel):
    """A fault applied to matching requests after skipping `after` of them, `count` times (or until cleared)."""

    mode: Literal["latency", "status", "error_rate", "timeout"]
    latency_ms: int = Field(default=0, ge=0, le=60000)
    status: int = Field(default=503, ge=500, le=599)
    error_rate: float = Field(default=1.0, ge=0, le=1)
    timeout_seconds: float = Field(default=30.0, ge=0, le=120)
    path_prefix: str = "/cases"
    method: str | None = None
    after: int = Field(default=0, ge=0)
    count: int | None = Field(default=None, ge=1)


class FaultState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.config: FaultConfig | None = None
        self.seen = 0
        self.applied = 0

    def set(self, config: FaultConfig | None) -> None:
        with self._lock:
            self.config, self.seen, self.applied = config, 0, 0

    def take(self, method: str, path: str) -> FaultConfig | None:
        with self._lock:
            config = self.config
            if config is None or not path.startswith(config.path_prefix):
                return None
            if config.method and config.method.upper() != method.upper():
                return None
            self.seen += 1
            if self.seen <= config.after:
                return None
            if config.mode == "error_rate" and random.random() >= config.error_rate:
                return None
            self.applied += 1
            if config.count is not None and self.applied >= config.count:
                self.config = None
            return config

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return {"fault": self.config.model_dump() if self.config else None, "seen": self.seen,
                    "applied": self.applied}


FAULTS = FaultState()


@asynccontextmanager
async def lifespan(_: FastAPI):
    added = seed_missing()
    print(f"case API ready ({'seeded ' + str(added) + ' cases' if added else 'existing data'}) at {DB_PATH}")
    yield


app = FastAPI(title="Taco Alley mock case-management API", lifespan=lifespan)


@app.middleware("http")
async def inject_faults(request: Request, call_next):
    fault = FAULTS.take(request.method, request.url.path)
    if fault is not None:
        if fault.mode == "latency":
            await asyncio.sleep(fault.latency_ms / 1000)
        elif fault.mode == "timeout":
            await asyncio.sleep(fault.timeout_seconds)
            return JSONResponse({"detail": "injected timeout"}, status_code=504)
        else:
            return JSONResponse({"detail": f"injected fault ({fault.mode})"}, status_code=fault.status)
    return await call_next(request)


def parse_scope(scope: str | None) -> set[str]:
    stores = {item.strip() for item in (scope or "").split(",") if item.strip()}
    if not stores:
        raise HTTPException(status_code=400, detail="X-Store-Scope header is required")
    return stores


def case_record(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    actions = connection.execute(
        "SELECT action_id, type, amount, recorded_at FROM case_actions WHERE case_id = ? ORDER BY recorded_at, action_id",
        (row["case_id"],)).fetchall()
    return {**dict(row), "recorded_actions": [dict(action) for action in actions]}


def scoped_case(connection: sqlite3.Connection, case_id: str, stores: set[str]) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    # Out-of-scope cases are indistinguishable from missing ones.
    if row is None or row["store_id"] not in stores:
        raise HTTPException(status_code=404, detail="case not found")
    return row


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    with closing(connect()) as connection:
        return {"status": "ok", "cases": connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0]}


@app.get("/cases/{case_id}")
def get_case(case_id: str, x_store_scope: str | None = Header(default=None)) -> dict[str, Any]:
    stores = parse_scope(x_store_scope)
    with closing(connect()) as connection:
        return case_record(connection, scoped_case(connection, case_id, stores))


@app.get("/cases")
def find_cases(order_id: str = Query(...), x_store_scope: str | None = Header(default=None)) -> list[dict[str, Any]]:
    stores = parse_scope(x_store_scope)
    with closing(connect()) as connection:
        rows = connection.execute("SELECT * FROM cases WHERE order_id = ?", (order_id,)).fetchall()
        return [case_record(connection, row) for row in rows if row["store_id"] in stores]


class ActionRequest(BaseModel):
    type: Literal[ACTION_TYPES]  # type: ignore[valid-type]
    amount: float = Field(ge=0)
    proposal_version: int = Field(ge=1)
    approved_by: str = Field(min_length=1, max_length=64)


@app.post("/cases/{case_id}/actions")
def record_action(case_id: str, action: ActionRequest, x_store_scope: str | None = Header(default=None),
                  idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    """Record an approved action exactly once per idempotency key."""
    stores = parse_scope(x_store_scope)
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    request_hash = hashlib.sha256(json.dumps(action.model_dump(), sort_keys=True).encode()).hexdigest()
    with closing(connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = scoped_case(connection, case_id, stores)
        existing = connection.execute("SELECT * FROM action_keys WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if existing is not None:
            if existing["case_id"] != case_id or existing["request_hash"] != request_hash:
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used with a different request")
            stored = connection.execute("SELECT action_id, type, amount, recorded_at FROM case_actions WHERE action_id = ?",
                                        (existing["action_id"],)).fetchone()
            return JSONResponse({"replayed": True, "action": dict(stored), "case": case_record(connection, row)})
        monetary = connection.execute("SELECT action_id FROM case_actions WHERE case_id = ? AND amount > 0",
                                      (case_id,)).fetchall()
        if action.amount > 0 and monetary:
            raise HTTPException(status_code=409, detail={"conflict": "case already has a monetary action",
                                                         "action_ids": [r["action_id"] for r in monetary]})
        action_id = f"APR-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:10]}"
        recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute("INSERT INTO case_actions VALUES (?, ?, ?, ?, ?)",
                           (action_id, case_id, action.type, action.amount, recorded_at))
        connection.execute("INSERT INTO action_keys VALUES (?, ?, ?, ?)", (idempotency_key, case_id, request_hash, action_id))
        status = "escalated" if action.type == "escalate_to_manager" else "resolved"
        connection.execute("UPDATE cases SET status = ? WHERE case_id = ?", (status, case_id))
        row = connection.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        return JSONResponse({"replayed": False, "action": {"action_id": action_id, "type": action.type,
                                                            "amount": action.amount, "recorded_at": recorded_at},
                             "case": case_record(connection, row)}, status_code=201)


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    token = os.environ.get("TACO_ADMIN_TOKEN", "")
    if not token:
        raise HTTPException(status_code=404, detail="Not Found")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, token):
        raise HTTPException(status_code=403, detail="admin token required")


@app.get("/admin/faults", dependencies=[Depends(require_admin)])
def get_faults() -> dict[str, Any]:
    return FAULTS.describe()


@app.put("/admin/faults", dependencies=[Depends(require_admin)])
def put_faults(config: FaultConfig) -> dict[str, Any]:
    FAULTS.set(config)
    return FAULTS.describe()


@app.delete("/admin/faults", dependencies=[Depends(require_admin)])
def delete_faults() -> dict[str, Any]:
    FAULTS.set(None)
    return FAULTS.describe()


@app.post("/admin/reset", dependencies=[Depends(require_admin)])
def reset_cases(case_ids: list[str] | None = Body(default=None, embed=True)) -> dict[str, Any]:
    """Restore cases to the generated fixtures: drop approved actions and reset status."""
    seeded = {c["case_id"]: c for c in load_json(GENERATED_DIR / "cases.json")}
    targets = [cid for cid in (case_ids or list(seeded)) if cid in seeded]
    with closing(connect()) as connection, connection:
        for case_id in targets:
            connection.execute("DELETE FROM case_actions WHERE case_id = ? AND action_id LIKE 'APR-%'", (case_id,))
            connection.execute("DELETE FROM action_keys WHERE case_id = ?", (case_id,))
            connection.execute("UPDATE cases SET status = ?, notes = ?, order_id = ? WHERE case_id = ?",
                               (seeded[case_id]["status"], seeded[case_id]["notes"], seeded[case_id]["order_id"], case_id))
    return {"reset": len(targets)}

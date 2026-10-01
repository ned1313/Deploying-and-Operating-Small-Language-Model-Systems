"""Mock case-management API. Owns its own SQLite file; seeds it from the generated cases on first start.

Run: uvicorn main:app --host 0.0.0.0 --port 8081
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query

from taco_shared.paths import GENERATED_DIR, load_json

DB_PATH = Path(os.environ.get("TACO_CASE_DB", "/data/cases/cases.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (case_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, customer_id TEXT NOT NULL,
                                  store_id TEXT NOT NULL, status TEXT NOT NULL, opened_at TEXT NOT NULL, notes TEXT);
CREATE TABLE IF NOT EXISTS case_actions (action_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(case_id),
                                         type TEXT NOT NULL, amount REAL NOT NULL, recorded_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cases_order ON cases (order_id);
"""
# Module 3 adds an idempotent POST /cases/{case_id}/actions and fault injection here.


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def seed_if_empty() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as connection, connection:
        connection.executescript(SCHEMA)
        if connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0]:
            return 0
        cases = load_json(GENERATED_DIR / "cases.json")
        connection.executemany("INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?)", [
            (c["case_id"], c["order_id"], c["customer_id"], c["store_id"], c["status"], c["opened_at"], c["notes"])
            for c in cases])
        connection.executemany("INSERT INTO case_actions VALUES (?, ?, ?, ?, ?)", [
            (a["action_id"], c["case_id"], a["type"], a["amount"], a["recorded_at"])
            for c in cases for a in c["recorded_actions"]])
        return len(cases)


@asynccontextmanager
async def lifespan(_: FastAPI):
    seeded = seed_if_empty()
    print(f"case API ready ({'seeded ' + str(seeded) + ' cases' if seeded else 'existing data'}) at {DB_PATH}")
    yield


app = FastAPI(title="Taco Alley mock case-management API", lifespan=lifespan)


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


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    with closing(connect()) as connection:
        return {"status": "ok", "cases": connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0]}


@app.get("/cases/{case_id}")
def get_case(case_id: str, x_store_scope: str | None = Header(default=None)) -> dict[str, Any]:
    stores = parse_scope(x_store_scope)
    with closing(connect()) as connection:
        row = connection.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        # Out-of-scope cases are indistinguishable from missing ones.
        if row is None or row["store_id"] not in stores:
            raise HTTPException(status_code=404, detail="case not found")
        return case_record(connection, row)


@app.get("/cases")
def find_cases(order_id: str = Query(...), x_store_scope: str | None = Header(default=None)) -> list[dict[str, Any]]:
    stores = parse_scope(x_store_scope)
    with closing(connect()) as connection:
        rows = connection.execute("SELECT * FROM cases WHERE order_id = ?", (order_id,)).fetchall()
        return [case_record(connection, row) for row in rows if row["store_id"] in stores]

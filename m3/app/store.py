"""Application records for the complaint API (app.db). The workflow's own state lives in checkpoints.db."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, store_id TEXT NOT NULL, customer_id TEXT NOT NULL,
    submitted_by TEXT NOT NULL, scenario_id TEXT, status TEXT NOT NULL, outcome_reason TEXT,
    proposal_version INTEGER NOT NULL DEFAULT 0, action_json TEXT, review_requested_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_request_id TEXT);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, proposal_version INTEGER NOT NULL,
    decision TEXT NOT NULL, reviewer TEXT NOT NULL, note TEXT, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cases_status ON cases (status);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AppStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._connection:
            self._connection.executescript(SCHEMA)

    def get(self, case_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        return self._decode(row)

    def list(self, store_ids: tuple[str, ...], status: str | None = None) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in store_ids)
        query = f"SELECT * FROM cases WHERE store_id IN ({marks})"
        params: list[Any] = list(store_ids)
        if status:
            query += " AND status = ?"
            params.append(status)
        with self._lock:
            rows = self._connection.execute(query + " ORDER BY updated_at DESC", params).fetchall()
        return [self._decode(row) for row in rows]  # type: ignore[misc]

    def by_status(self, status: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM cases WHERE status = ?", (status,)).fetchall()
        return [self._decode(row) for row in rows]  # type: ignore[misc]

    def case_ids(self) -> list[str]:
        with self._lock:
            return [row[0] for row in self._connection.execute("SELECT case_id FROM cases").fetchall()]

    def create(self, case_id: str, thread_id: str, store_id: str, customer_id: str, submitted_by: str,
               scenario_id: str | None, request_id: str) -> None:
        stamp = now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO cases (case_id, thread_id, store_id, customer_id, submitted_by, scenario_id, status, "
                "created_at, updated_at, last_request_id) VALUES (?, ?, ?, ?, ?, ?, 'investigating', ?, ?, ?)",
                (case_id, thread_id, store_id, customer_id, submitted_by, scenario_id, stamp, stamp, request_id))

    def update(self, case_id: str, **fields: Any) -> None:
        if "action" in fields:
            action = fields.pop("action")
            fields["action_json"] = json.dumps(action) if action is not None else None
        fields["updated_at"] = now()
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self._lock, self._connection:
            self._connection.execute(f"UPDATE cases SET {columns} WHERE case_id = ?", [*fields.values(), case_id])

    def add_review(self, case_id: str, version: int, decision: str, reviewer: str, note: str | None) -> str:
        stamp = now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO reviews (case_id, proposal_version, decision, reviewer, note, at) VALUES (?, ?, ?, ?, ?, ?)",
                (case_id, version, decision, reviewer, note, stamp))
        return stamp

    def reviews(self, case_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("SELECT proposal_version, decision, reviewer, note, at FROM reviews "
                                            "WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()
        return [dict(row) for row in rows]

    def delete(self, case_id: str) -> str | None:
        """Remove a case and its reviews; returns the thread id so its checkpoints can be deleted too."""
        row = self.get(case_id)
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM reviews WHERE case_id = ?", (case_id,))
            self._connection.execute("DELETE FROM cases WHERE case_id = ?", (case_id,))
        return row["thread_id"] if row else None

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        record["action"] = json.loads(record.pop("action_json")) if record.get("action_json") else None
        return record

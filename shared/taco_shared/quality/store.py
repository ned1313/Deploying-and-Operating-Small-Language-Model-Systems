"""The evaluation store (quality.db on the taco-app-quality volume).

Planning decision 4a: one SQLite file, written by the complaint API (samples, reviews from the review
page) and by the `quality` tool (scores, reviews from the CLI), each in its own tables. WAL mode and a
busy timeout keep the two writers from failing on each other's short transactions.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, request_id TEXT, actor_id TEXT, store_id TEXT,
    scenario_id TEXT, status TEXT NOT NULL, outcome_reason TEXT, sampled_reason TEXT NOT NULL, model TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, record_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS samples_case ON samples (case_id);
CREATE INDEX IF NOT EXISTS samples_updated ON samples (updated_at);
CREATE TABLE IF NOT EXISTS scores (
    sample_id TEXT PRIMARY KEY, scored_at TEXT NOT NULL, sample_updated_at TEXT NOT NULL, model TEXT,
    status TEXT, checks_json TEXT NOT NULL, checks_passed INTEGER, label_json TEXT, label_passed INTEGER,
    successful INTEGER, escalated INTEGER NOT NULL DEFAULT 0, flagged INTEGER NOT NULL DEFAULT 0,
    flag_reasons_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id TEXT NOT NULL, reviewer TEXT NOT NULL, tone INTEGER,
    helpfulness INTEGER, escalation INTEGER, safe_to_send INTEGER, disposition TEXT NOT NULL, note TEXT,
    at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS reviews_sample ON reviews (sample_id);
"""

DISPOSITIONS = ("ok", "model_quality", "policy_gap", "infrastructure", "label_error")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bool(value: Any) -> int | None:
    return None if value is None else int(bool(value))


class QualityStore:
    def __init__(self, path: Path, read_only: bool = False) -> None:
        self.path = path
        if read_only:
            self._connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=5)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        if not read_only:
            with self._lock, self._connection:
                self._connection.execute("PRAGMA journal_mode=WAL")
                self._connection.executescript(SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def _rows(self, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(query, params).fetchall()

    # Samples (written by the complaint API) ------------------------------------------------------

    def has_sample(self, sample_id: str) -> bool:
        return bool(self._rows("SELECT 1 FROM samples WHERE sample_id = ?", (sample_id,)))

    def upsert_sample(self, record: dict[str, Any], sampled_reason: str) -> None:
        stamp = now()
        values = (record["sample_id"], record["case_id"], record.get("request_id"), record.get("actor_id"),
                  record.get("store_id"), record.get("scenario_id"), record["status"], record.get("outcome_reason"),
                  sampled_reason, (record.get("versions") or {}).get("model"), stamp, stamp,
                  json.dumps(record, default=str))
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO samples (sample_id, case_id, request_id, actor_id, store_id, scenario_id, status, "
                "outcome_reason, sampled_reason, model, created_at, updated_at, record_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(sample_id) DO UPDATE SET "
                "request_id = excluded.request_id, status = excluded.status, outcome_reason = excluded.outcome_reason, "
                "model = excluded.model, updated_at = excluded.updated_at, record_json = excluded.record_json",
                values)

    def sample(self, sample_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM samples WHERE sample_id = ? OR case_id = ? ORDER BY updated_at DESC "
                          "LIMIT 1", (sample_id, sample_id))
        return self._decode(rows[0]) if rows else None

    def samples_to_score(self, rescore: bool = False) -> Iterator[dict[str, Any]]:
        query = ("SELECT s.* FROM samples s LEFT JOIN scores c ON c.sample_id = s.sample_id"
                 + ("" if rescore else " WHERE c.sample_id IS NULL OR c.sample_updated_at < s.updated_at")
                 + " ORDER BY s.updated_at")
        for row in self._rows(query):
            yield self._decode(row)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        record = json.loads(row["record_json"])
        record.update(sample_id=row["sample_id"], sampled_reason=row["sampled_reason"], created_at=row["created_at"],
                      updated_at=row["updated_at"], status=row["status"], outcome_reason=row["outcome_reason"])
        return record

    # Scores (written by the quality tool) --------------------------------------------------------

    def save_score(self, sample: dict[str, Any], checks: dict[str, Any], checks_passed: bool | None,
                   label: dict[str, Any] | None, successful: bool | None, flag_reasons: list[str]) -> None:
        escalated = sample["status"] == "escalated" and not sample.get("actions")
        values = (sample["sample_id"], now(), sample["updated_at"], (sample.get("versions") or {}).get("model"),
                  sample["status"], json.dumps(checks), _bool(checks_passed), json.dumps(label) if label else None,
                  _bool(label["passed"]) if label else None, _bool(successful), int(escalated), int(bool(flag_reasons)),
                  json.dumps(flag_reasons))
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO scores (sample_id, scored_at, sample_updated_at, model, status, checks_json, "
                "checks_passed, label_json, label_passed, successful, escalated, flagged, flag_reasons_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)

    def score(self, sample_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM scores WHERE sample_id = ?", (sample_id,))
        if not rows:
            return None
        row = dict(rows[0])
        row["checks"] = json.loads(row.pop("checks_json"))
        row["label"] = json.loads(row.pop("label_json")) if row["label_json"] else None
        row["flag_reasons"] = json.loads(row.pop("flag_reasons_json"))
        return row

    def scored(self, since: str | None = None, until: str | None = None, model: str | None = None,
               case_ids: set[str] | None = None) -> list[dict[str, Any]]:
        """Scored samples joined with their sample columns, oldest first."""
        query = ("SELECT c.*, s.case_id, s.scenario_id, s.created_at, s.updated_at, s.sampled_reason, "
                 "s.outcome_reason FROM scores c JOIN samples s ON s.sample_id = c.sample_id WHERE 1 = 1")
        params: list[Any] = []
        if since:
            query, params = query + " AND s.created_at >= ?", [*params, since]
        if until:
            query, params = query + " AND s.created_at <= ?", [*params, until]
        if model:
            query, params = query + " AND c.model = ?", [*params, model]
        rows = [dict(row) for row in self._rows(query + " ORDER BY s.created_at", tuple(params))]
        for row in rows:
            row["checks"] = json.loads(row.pop("checks_json"))
            row["label"] = json.loads(row.pop("label_json")) if row["label_json"] else None
            row["flag_reasons"] = json.loads(row.pop("flag_reasons_json"))
        return [row for row in rows if case_ids is None or row["case_id"] in case_ids]

    # Reviews (written by the review page and the quality tool) -----------------------------------

    def add_review(self, sample_id: str, reviewer: str, tone: int | None, helpfulness: int | None,
                   escalation: int | None, safe_to_send: bool | None, disposition: str, note: str | None) -> None:
        if disposition not in DISPOSITIONS:
            raise ValueError(f"disposition must be one of {DISPOSITIONS}")
        for name, value in (("tone", tone), ("helpfulness", helpfulness), ("escalation", escalation)):
            if value is not None and not 1 <= value <= 5:
                raise ValueError(f"{name} must be 1-5")
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO reviews (sample_id, reviewer, tone, helpfulness, escalation, safe_to_send, disposition, "
                "note, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sample_id, reviewer, tone, helpfulness, escalation, _bool(safe_to_send), disposition, note, now()))

    def reviews(self, sample_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows("SELECT * FROM reviews WHERE sample_id = ? ORDER BY id", (sample_id,))]

    def queue(self, limit: int = 100) -> list[dict[str, Any]]:
        """Flagged samples without a review, newest first."""
        rows = self._rows(
            "SELECT c.sample_id, c.model, c.status, c.flag_reasons_json, c.checks_passed, c.label_passed, s.case_id, "
            "s.scenario_id, s.request_id, s.updated_at FROM scores c JOIN samples s ON s.sample_id = c.sample_id "
            "WHERE c.flagged = 1 AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.sample_id = c.sample_id) "
            "ORDER BY s.updated_at DESC LIMIT ?", (limit,))
        result = []
        for row in rows:
            item = dict(row)
            item["flag_reasons"] = json.loads(item.pop("flag_reasons_json"))
            result.append(item)
        return result

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """The last `limit` scored samples with a final outcome, newest first (for degradation signals)."""
        rows = self._rows(
            "SELECT c.model, c.checks_passed, c.label_passed, c.successful, c.escalated, c.status, s.sampled_reason "
            "FROM scores c JOIN samples s ON s.sample_id = c.sample_id WHERE c.successful IS NOT NULL "
            "ORDER BY s.updated_at DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, Any]:
        """Totals for the metrics endpoint."""
        samples = {row["status"]: row["n"] for row in self._rows("SELECT status, COUNT(*) AS n FROM samples GROUP BY status")}
        scored = [dict(row) for row in self._rows(
            "SELECT COALESCE(model, 'unknown') AS model, successful, COUNT(*) AS n FROM scores GROUP BY model, successful")]
        queue = self._rows("SELECT COUNT(*) AS n FROM scores c WHERE c.flagged = 1 AND NOT EXISTS "
                           "(SELECT 1 FROM reviews r WHERE r.sample_id = c.sample_id)")[0]["n"]
        unscored = self._rows("SELECT COUNT(*) AS n FROM samples s LEFT JOIN scores c ON c.sample_id = s.sample_id "
                              "WHERE c.sample_id IS NULL OR c.sample_updated_at < s.updated_at")[0]["n"]
        reviews = self._rows("SELECT COUNT(*) AS n FROM reviews")[0]["n"]
        return {"samples": samples, "scored": scored, "queue": queue, "unscored": unscored, "reviews": reviews}

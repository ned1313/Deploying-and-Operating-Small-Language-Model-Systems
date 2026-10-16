"""The audit store (audit.db on the taco-app-audit volume), written only by the complaint API.

`events` is append-only metadata: no complaint text, tool results, or model output. Each event carries
the hash of the previous one, so `verify()` detects edited, inserted, or deleted rows. Captured content
lives in `content`, encrypted with Fernet (key in a runtime-mounted secret file) and referenced from the
event by id and SHA-256. Deleting a case's content keeps its events and their hashes intact.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, at TEXT NOT NULL, kind TEXT NOT NULL,
    request_id TEXT, case_id TEXT, thread_id TEXT, actor_id TEXT, role TEXT, service TEXT,
    metadata_json TEXT NOT NULL, content_id TEXT, content_sha256 TEXT, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_case ON events (case_id);
CREATE TABLE IF NOT EXISTS content (
    content_id TEXT PRIMARY KEY, case_id TEXT, created_at TEXT NOT NULL, ciphertext BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS content_case ON content (case_id);
CREATE TABLE IF NOT EXISTS cursors (thread_id TEXT PRIMARY KEY, steps INTEGER NOT NULL, evidence INTEGER NOT NULL);
"""

GENESIS = "0" * 64
HASHED_FIELDS = ("seq", "event_id", "at", "kind", "request_id", "case_id", "thread_id", "actor_id", "role", "service",
                 "metadata_json", "content_id", "content_sha256", "prev_hash")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def event_hash(row: dict[str, Any]) -> str:
    return hashlib.sha256(canonical({name: row.get(name) for name in HASHED_FIELDS}).encode()).hexdigest()


def load_key(path: str | Path | None) -> bytes | None:
    if not path:
        return None
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value.encode() if value else None


class AuditStore:
    def __init__(self, path: Path, key: bytes, service: str = "complaint-api") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.service = path, service
        self._fernet = Fernet(key)
        self._connection = sqlite3.connect(path, check_same_thread=False, timeout=10, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(SCHEMA)

    def close(self) -> None:
        self._connection.close()

    # Writing ------------------------------------------------------------------------------------

    def append(self, kind: str, *, case_id: str | None = None, request_id: str | None = None,
               thread_id: str | None = None, actor_id: str | None = None, role: str | None = None,
               metadata: dict[str, Any] | None = None, content: Any = None) -> dict[str, Any]:
        """Append one event (and its encrypted content); one serialized transaction keeps the chain linear."""
        content_id = content_sha = None
        ciphertext = None
        if content is not None:
            plain = canonical(content).encode()
            content_id, content_sha = uuid.uuid4().hex, hashlib.sha256(plain).hexdigest()
            ciphertext = self._fernet.encrypt(plain)
        row = {"event_id": uuid.uuid4().hex, "at": now(), "kind": kind, "request_id": request_id, "case_id": case_id,
               "thread_id": thread_id, "actor_id": actor_id, "role": role, "service": self.service,
               "metadata_json": canonical(metadata or {}), "content_id": content_id, "content_sha256": content_sha}
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                last = connection.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                row["seq"] = (last["seq"] if last else 0) + 1
                row["prev_hash"] = last["hash"] if last else GENESIS
                row["hash"] = event_hash(row)
                if ciphertext is not None:
                    connection.execute("INSERT INTO content (content_id, case_id, created_at, ciphertext) "
                                       "VALUES (?, ?, ?, ?)", (content_id, case_id, row["at"], ciphertext))
                columns = (*HASHED_FIELDS, "hash")
                connection.execute(f"INSERT INTO events ({', '.join(columns)}) VALUES "
                                   f"({', '.join('?' for _ in columns)})", tuple(row[c] for c in columns))
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return self._public(row)

    def cursor(self, thread_id: str) -> tuple[int, int]:
        with self._lock:
            row = self._connection.execute("SELECT steps, evidence FROM cursors WHERE thread_id = ?",
                                           (thread_id,)).fetchone()
        return (row["steps"], row["evidence"]) if row else (0, 0)

    def set_cursor(self, thread_id: str, steps: int, evidence: int) -> None:
        with self._lock:
            self._connection.execute("INSERT INTO cursors (thread_id, steps, evidence) VALUES (?, ?, ?) "
                                     "ON CONFLICT(thread_id) DO UPDATE SET steps = excluded.steps, "
                                     "evidence = excluded.evidence", (thread_id, steps, evidence))

    def delete_case_content(self, case_id: str) -> int:
        """Remove the case's captured content. Events (and the chain) stay; their content ids now dangle."""
        with self._lock:
            return self._connection.execute("DELETE FROM content WHERE case_id = ?", (case_id,)).rowcount

    # Reading ------------------------------------------------------------------------------------

    @staticmethod
    def _public(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json"))
        return data

    def events(self, case_id: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        query = "SELECT * FROM events" + (" WHERE case_id = ?" if case_id else "") + " ORDER BY seq LIMIT ?"
        params: tuple[Any, ...] = (case_id, limit) if case_id else (limit,)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [self._public(row) for row in rows]

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        """Newest events first, metadata only."""
        with self._lock:
            rows = self._connection.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [self._public(row) for row in rows]

    def content(self, content_id: str) -> Any:
        """Decrypted content, or None when it was deleted."""
        with self._lock:
            row = self._connection.execute("SELECT ciphertext FROM content WHERE content_id = ?",
                                           (content_id,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(self._fernet.decrypt(row["ciphertext"]))
        except InvalidToken:
            return {"error": "content cannot be decrypted with the configured key"}

    def verify(self) -> dict[str, Any]:
        """Recompute the hash chain; report the first event that does not match."""
        with self._lock:
            rows = self._connection.execute("SELECT * FROM events ORDER BY seq").fetchall()
        previous, expected_seq = GENESIS, 1
        for row in rows:
            data = dict(row)
            if data["seq"] != expected_seq or data["prev_hash"] != previous or event_hash(data) != data["hash"]:
                return {"ok": False, "events": len(rows), "first_bad_seq": data["seq"]}
            previous, expected_seq = data["hash"], expected_seq + 1
        return {"ok": True, "events": len(rows), "head": previous}

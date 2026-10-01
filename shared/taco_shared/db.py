"""Read-only access to the application SQLite database. Only narrow, scoped queries are exposed."""

from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

DEFAULT_APP_DB = Path(os.environ.get("TACO_APP_DB", "/data/app/taco_app.db"))


class AppDatabase:
    def __init__(self, path: Path = DEFAULT_APP_DB) -> None:
        if not Path(path).is_file():
            raise FileNotFoundError(f"application database not found at {path}; run the seed service first")
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def meta(self) -> dict[str, str]:
        with self._connect() as connection:
            return {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}

    def get_order(self, order_id: str, customer_id: str, store_ids: tuple[str, ...]) -> dict[str, Any] | None:
        """Return the order only if it belongs to the customer and one of the permitted stores."""
        placeholders = ",".join("?" for _ in store_ids)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT o.*, s.name AS store_name FROM orders o JOIN stores s ON s.store_id = o.store_id "
                f"WHERE o.order_id = ? AND o.customer_id = ? AND o.store_id IN ({placeholders})",
                (order_id, customer_id, *store_ids),
            ).fetchone()
            if row is None:
                return None
            items = connection.execute(
                "SELECT name, quantity, unit_price FROM order_items WHERE order_id = ? ORDER BY line_no", (order_id,)
            ).fetchall()
        order = dict(row)
        order["items"] = [{"name": i["name"], "quantity": i["quantity"], "unit_price": i["unit_price"],
                           "line_total": round(i["quantity"] * i["unit_price"], 2)} for i in items]
        return order

    def prior_resolutions(self, customer_id: str, as_of: str, lookback_days: int) -> list[dict[str, Any]]:
        start = (date.fromisoformat(as_of) - timedelta(days=lookback_days)).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT resolution_id, date, order_id, resolution_type, amount, reason FROM prior_resolutions "
                "WHERE customer_id = ? AND date >= ? AND date <= ? ORDER BY date, resolution_id",
                (customer_id, start, as_of),
            ).fetchall()
        return [dict(row) for row in rows]

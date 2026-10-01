"""Seed the application SQLite database from the generated dataset.

    python -m taco_shared.dataset.seed            # create if missing; no-op if already seeded with this build
    python -m taco_shared.dataset.seed --reset    # destructive: rebuild from the fixtures
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from pathlib import Path

from .. import paths
from ..db import DEFAULT_APP_DB
from ..paths import load_json

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE stores (store_id TEXT PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL, state TEXT NOT NULL,
                     tax_rate REAL NOT NULL);
CREATE TABLE customers (customer_id TEXT PRIMARY KEY, home_store_id TEXT NOT NULL REFERENCES stores(store_id));
CREATE TABLE orders (order_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(customer_id),
                     store_id TEXT NOT NULL REFERENCES stores(store_id), order_date TEXT NOT NULL, channel TEXT NOT NULL,
                     fulfillment TEXT NOT NULL, subtotal REAL NOT NULL, tax REAL NOT NULL, delivery_fee REAL NOT NULL,
                     total REAL NOT NULL, promised_delivery_time TEXT, actual_delivery_time TEXT);
CREATE TABLE order_items (order_id TEXT NOT NULL REFERENCES orders(order_id), line_no INTEGER NOT NULL,
                          name TEXT NOT NULL, quantity INTEGER NOT NULL, unit_price REAL NOT NULL,
                          PRIMARY KEY (order_id, line_no));
CREATE TABLE prior_resolutions (resolution_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, date TEXT NOT NULL,
                                order_id TEXT NOT NULL, resolution_type TEXT NOT NULL, amount REAL NOT NULL,
                                reason TEXT NOT NULL);
CREATE INDEX prior_resolutions_customer ON prior_resolutions (customer_id, date);
"""
SOURCE_FILES = ("customers.json", "orders.json", "prior_resolutions.json", "build_report.json")


def fixture_digest() -> str:
    digest = hashlib.sha256()
    for name in SOURCE_FILES:
        digest.update((paths.GENERATED_DIR / name).read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def existing_digest(path: Path) -> str | None:
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = 'fixture_sha256'").fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def write_database(path: Path, digest: str) -> dict[str, int]:
    stores = load_json(paths.DATA_DIR / "stores.json")["stores"]
    customers = load_json(paths.GENERATED_DIR / "customers.json")
    orders = load_json(paths.GENERATED_DIR / "orders.json")
    priors = load_json(paths.GENERATED_DIR / "prior_resolutions.json")
    report = load_json(paths.GENERATED_DIR / "build_report.json")
    temp = path.with_suffix(".tmp")
    temp.unlink(missing_ok=True)
    with sqlite3.connect(temp) as connection:
        # DELETE journal mode keeps the file usable from read-only mounts.
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.executescript(SCHEMA)
        connection.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("fixture_sha256", digest), ("policy_version", report["policy_version"]),
            ("agent_version", report["agent_version"]), ("source_sha256_lf", report["source_sha256_lf"]),
        ])
        connection.executemany("INSERT INTO stores VALUES (?, ?, ?, ?, ?)",
                               [(s["store_id"], s["name"], s["location"], s["state"], s["tax_rate"]) for s in stores])
        connection.executemany("INSERT INTO customers VALUES (?, ?)",
                               [(c["customer_id"], c["home_store_id"]) for c in customers])
        connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (o["order_id"], o["customer_id"], o["store_id"], o["order_date"], o["channel"], o["fulfillment"],
             o["subtotal"], o["tax"], o["delivery_fee"], o["total"], o["promised_delivery_time"],
             o["actual_delivery_time"]) for o in orders])
        connection.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)", [
            (o["order_id"], line, item["name"], item["quantity"], item["unit_price"])
            for o in orders for line, item in enumerate(o["items"], start=1)])
        connection.executemany("INSERT INTO prior_resolutions VALUES (?, ?, ?, ?, ?, ?, ?)", [
            (p["resolution_id"], p["customer_id"], p["date"], p["order_id"], p["resolution_type"], p["amount"],
             p["reason"]) for p in priors])
    connection.close()
    os.replace(temp, path)
    return {"stores": len(stores), "customers": len(customers), "orders": len(orders), "prior_resolutions": len(priors)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_APP_DB)
    parser.add_argument("--reset", action="store_true", help="Destructive: replace the database with the fixtures.")
    args = parser.parse_args()
    digest = fixture_digest()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.db.exists() and not args.reset:
        current = existing_digest(args.db)
        if current == digest:
            print(f"{args.db} is already seeded with this dataset build; nothing to do.")
            return 0
        print(f"{args.db} exists but was seeded from a different dataset build. "
              "Re-run with --reset to replace it (destructive).", file=sys.stderr)
        return 2
    counts = write_database(args.db, digest)
    print(f"{'Reset' if args.reset else 'Seeded'} {args.db}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

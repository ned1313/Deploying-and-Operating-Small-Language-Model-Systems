#!/usr/bin/env python3
"""Explicit, destructive reset of the demo data (module 6). Stopping containers never does this.

    podman-compose stop complaint-api case-api
    podman-compose run --rm reset            # lists what would be deleted, deletes nothing
    podman-compose run --rm reset --yes      # deletes it, then reseeds the fixtures
    podman-compose up -d case-api complaint-api

Clears: workflow checkpoints and case records (taco-app-state), quality samples, scores, and reviews
(taco-app-quality), recorded mock actions (taco-case-data, reseeded by case-api on start), and the seeded
orders database (rebuilt from the fixtures). Never touches the audit store (taco-app-audit is not even
mounted here), Authelia's data, monitoring history, or the model cache on the GPU host.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from taco_shared.dataset.seed import DEFAULT_APP_DB, fixture_digest, write_database

TARGETS = {
    "workflow state": [Path("/data/state/app.db"), Path("/data/state/checkpoints.db")],
    "quality evaluation store": [Path("/data/quality/quality.db")],
    "mock case API records": [Path("/data/cases/cases.db")],
}
RUNNING = ("http://complaint-api:8090/healthz?remote=false", "http://case-api:8081/healthz")


def files(paths: list[Path]) -> list[Path]:
    """The database files and their SQLite WAL/shared-memory companions that exist."""
    found = []
    for path in paths:
        found += [p for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")) if p.exists()]
    return found


def still_running() -> list[str]:
    up = []
    for url in RUNNING:
        try:
            httpx.get(url, timeout=2.0)
            up.append(url.split("/")[2].split(":")[0])
        except httpx.HTTPError:
            pass
    return up


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="really delete (without it, only list)")
    args = parser.parse_args()

    running = still_running()
    if running:
        print(f"Refusing: {', '.join(running)} still running. Stop them first:  podman-compose stop complaint-api case-api")
        return 2
    plan = {name: files(paths) for name, paths in TARGETS.items()}
    for name, found in plan.items():
        print(f"{name}: " + (", ".join(str(p) for p in found) if found else "nothing to delete"))
    print(f"seeded orders and history: rebuild {DEFAULT_APP_DB} from the fixtures")
    print("kept: audit store, Authelia data, monitoring history, model cache")
    if not args.yes:
        print("\nNothing deleted. Re-run with --yes to reset.")
        return 0
    for found in plan.values():
        for path in found:
            path.unlink()
    write_database(DEFAULT_APP_DB, fixture_digest())
    print("\nReset done. Start the services again:  podman-compose up -d case-api complaint-api")
    return 0


if __name__ == "__main__":
    sys.exit(main())

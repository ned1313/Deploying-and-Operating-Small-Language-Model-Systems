"""Runs one headless Locust workload and records what is needed to reproduce and compare it.

    podman-compose run --rm load --mode direct --shape burst --label direct-burst
    podman-compose run --rm load --mode app --shape steady --label app-steady

Writes results/load/load-<label>-<ts>_stats.csv (and the other Locust CSVs), an HTML report, an outcomes
file, and a metadata file (-meta.json) that the baseline report reads for the run window.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from taco_shared.api_client import APP_GATEWAY_URL, Api, authelia_mode
from taco_shared.config import InferenceSettings
from taco_shared.load import LOAD_DATASET_VERSION, LOAD_SEED, load_cases
from taco_shared.reporting import safe_slug, timestamp_slug

RESULTS = Path(os.environ.get("RESULTS_DIR", "/app/results"))
NETWORK_PATHS = {
    "direct": "desktop container -> LAN -> gateway (nginx) -> vLLM",
    "app": "desktop container -> complaint-api (taco-app-private) -> LAN -> gateway (nginx) -> vLLM; "
           "tools -> case-api and local SQLite",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["direct", "app"], required=True)
    parser.add_argument("--shape", choices=["steady", "burst"], default="steady")
    parser.add_argument("--users", type=int, default=2)
    parser.add_argument("--burst-users", type=int, default=8)
    parser.add_argument("--duration", type=int, help="seconds (default: 180 steady, 240 burst)")
    parser.add_argument("--label", help="name for the result files (default: <mode>-<shape>)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-approve", action="store_true", help="app mode: leave proposals pending")
    parser.add_argument("--think-min", type=float, default=5.0, help="app mode: reviewer think time (s)")
    parser.add_argument("--think-max", type=float, default=15.0)
    parser.add_argument("--no-reset", action="store_true", help="app mode: do not reset the load-v1 cases first")
    parser.add_argument("--execution-host", default=os.environ.get("EXECUTION_HOST", "desktop"))
    return parser.parse_args()


def reset_load_cases() -> int:
    """Return load-v1 cases to their fixtures on both services so each run starts from the same state."""
    if not authelia_mode() and not os.environ.get("TACO_ADMIN_TOKEN"):
        sys.exit("TACO_ADMIN_TOKEN is not set; app mode resets the load cases through the admin routes")
    case_ids = [case["case_id"] for case in load_cases()]
    try:
        Api().reset(case_ids)
    except httpx.HTTPStatusError as error:
        sys.exit(f"resetting the load cases failed: HTTP {error.response.status_code} from {error.request.url.path}")
    return len(case_ids)


def main() -> int:
    args = parse_args()
    duration = args.duration or (240 if args.shape == "burst" else 180)
    label = safe_slug(args.label or f"{args.mode}-{args.shape}")
    prefix = RESULTS / f"load-{label}-{timestamp_slug()}"
    RESULTS.mkdir(parents=True, exist_ok=True)
    settings = InferenceSettings.from_env()
    app_url = APP_GATEWAY_URL if authelia_mode() else os.environ["COMPLAINT_API_URL"]
    host = settings.base_url if args.mode == "direct" else app_url
    if args.mode == "app" and not args.no_reset:
        print(f"reset {reset_load_cases()} {LOAD_DATASET_VERSION} cases", flush=True)

    env = {**os.environ, "TACO_MODE": args.mode, "TACO_SHAPE": args.shape, "TACO_USERS": str(args.users),
           "TACO_BURST_USERS": str(args.burst_users), "TACO_DURATION": str(duration), "TACO_SEED": str(args.seed),
           "TACO_APPROVE": "0" if args.no_approve else "1", "TACO_THINK_MIN": str(args.think_min),
           "TACO_THINK_MAX": str(args.think_max), "TACO_OUTCOMES_FILE": f"{prefix}-outcomes.json"}
    command = ["locust", "-f", str(Path(__file__).with_name("locustfile.py")), "--headless", "--host", host,
               "--csv", str(prefix), "--html", f"{prefix}.html", "--exit-code-on-error", "0", "--only-summary",
               "--stop-timeout", "30"]
    started = time.time()
    print(f"{args.mode}/{args.shape}: {args.users} users"
          + (f", {args.burst_users} during the burst" if args.shape == "burst" else "") + f", {duration}s -> {host}",
          flush=True)
    code = subprocess.call(command, env=env)
    ended = time.time()

    outcomes_path = Path(f"{prefix}-outcomes.json")
    metadata = {
        "label": label, "mode": args.mode, "shape": args.shape, "users": args.users,
        "burst_users": args.burst_users if args.shape == "burst" else None, "duration_seconds": duration,
        "seed": args.seed, "approve": not args.no_approve if args.mode == "app" else None,
        "think_seconds": [args.think_min, args.think_max] if args.mode == "app" else None,
        "dataset_version": LOAD_DATASET_VERSION if args.mode == "app" else "screening-v2 (module 1 prompts)",
        "dataset_seed": LOAD_SEED if args.mode == "app" else None,
        "target": host, "model": settings.model, "extra_body": settings.extra_body,
        "auth": {"inference_tls": settings.tls.enabled, "app_auth_mode": "authelia" if authelia_mode() else "header"},
        "execution_host": args.execution_host, "container_platform": platform.platform(),
        "network_path": NETWORK_PATHS[args.mode],
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
        "ended_at": datetime.fromtimestamp(ended, timezone.utc).isoformat(timespec="seconds"),
        "started_epoch": started, "ended_epoch": ended, "locust_exit_code": code,
        "files": {"stats_csv": f"{prefix.name}_stats.csv", "html": f"{prefix.name}.html",
                  "outcomes": outcomes_path.name},
        "outcomes": json.loads(outcomes_path.read_text()) if outcomes_path.exists() else {},
    }
    meta_path = Path(f"{prefix}-meta.json")
    meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"outcomes: {metadata['outcomes']}")
    print(f"wrote {meta_path}")
    return code


if __name__ == "__main__":
    sys.exit(main())

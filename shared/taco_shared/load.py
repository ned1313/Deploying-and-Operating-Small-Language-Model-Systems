"""Load-test workloads (module 4): a versioned selection of generated complaints and direct-inference prompts.

`load-v1` is a rule, not a file: labeled, non-curated complaints from the KOP and WG stores that have a
case, ordered by case id, then sampled with a fixed seed. The same rule always yields the same cases.
"""

from __future__ import annotations

import json
import random
from typing import Any

from .paths import DEFAULT_POLICY, DEFAULT_SCHEMA, DEFAULT_SCENARIOS, GENERATED_DIR, load_json
from .screening import build_messages, build_system_prompt

LOAD_DATASET_VERSION = "load-v1"
LOAD_SEED = 20261012
LOAD_SIZE = 150
STORE_REPS = {"KOP": "rep-kop-01", "WG": "rep-wg-01"}
LOAD_REVIEWER = "load-reviewer-01"


def load_cases(size: int = LOAD_SIZE, seed: int = LOAD_SEED) -> list[dict[str, Any]]:
    case_ids = {case["case_id"] for case in load_json(GENERATED_DIR / "cases.json")}
    rows = []
    with (GENERATED_DIR / "complaints.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if (row["store_id"] in STORE_REPS and row.get("scenario_id") is None
                    and row.get("label_status") == "labeled" and row.get("case_id") in case_ids):
                rows.append(row)
    rows.sort(key=lambda row: row["case_id"])
    chosen = random.Random(seed).sample(rows, min(size, len(rows)))
    return [{"case_id": row["case_id"], "customer_id": row["customer_id"], "complaint_date": row["complaint_date"],
             "complaint_channel": row["complaint_channel"], "complaint": row["complaint"], "store_id": row["store_id"],
             "actor_id": STORE_REPS[row["store_id"]], "category": row.get("policy_category")} for row in chosen]


def direct_requests(model: str, extra_body: dict[str, Any] | None = None, max_tokens: int = 800) -> list[dict[str, Any]]:
    """Chat completion bodies for the module 1 screening scenarios: facts inlined, json_schema output."""
    schema = load_json(DEFAULT_SCHEMA)
    system_prompt = build_system_prompt(DEFAULT_POLICY.read_text(encoding="utf-8"), schema)
    bodies = []
    for scenario in load_json(DEFAULT_SCENARIOS)["scenarios"]:
        body = {"model": model, "messages": build_messages(scenario, system_prompt), "temperature": 0,
                "max_tokens": max_tokens, "seed": 42,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "resolution_proposal", "schema": schema, "strict": True}}}
        bodies.append({"scenario_id": scenario["scenario_id"], "body": {**body, **(extra_body or {})}})
    return bodies

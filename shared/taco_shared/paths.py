"""Locations of shared data files. Override the data root with TACO_DATA_DIR."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("TACO_DATA_DIR") or Path(__file__).resolve().parent / "data")
SOURCE_CSV = DATA_DIR / "source" / "taco_alley_customer_complaints.csv"
POLICY_DIR = DATA_DIR / "policy"
SCHEMAS_DIR = DATA_DIR / "schemas"
CURATED_SCENARIOS = DATA_DIR / "scenarios" / "curated.json"
GENERATED_DIR = DATA_DIR / "generated"

DEFAULT_POLICY_VERSION = "2026.09"
DEFAULT_SCENARIOS = GENERATED_DIR / "screening_scenarios.json"
AGENT_SCENARIOS = GENERATED_DIR / "agent_scenarios.json"
DEFAULT_SCHEMA = SCHEMAS_DIR / "proposal.v1.json"
PROPOSAL_SCHEMA_V2 = SCHEMAS_DIR / "proposal.v2.json"
DEFAULT_POLICY = POLICY_DIR / f"{DEFAULT_POLICY_VERSION}.txt"
DEFAULT_RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "results"))


def policy_json_path(version: str = DEFAULT_POLICY_VERSION) -> Path:
    return POLICY_DIR / f"{version}.json"


def policy_text_path(version: str = DEFAULT_POLICY_VERSION) -> Path:
    return POLICY_DIR / f"{version}.txt"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

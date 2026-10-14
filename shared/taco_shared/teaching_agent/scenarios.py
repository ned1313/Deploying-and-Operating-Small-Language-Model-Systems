"""Scenario lookup and runtime wiring shared by the teaching script and the evaluation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from taco_shared.agent.tools import build_investigation_tools
from taco_shared.case_api_client import CaseApiClient
from taco_shared.config import InferenceSettings
from taco_shared.context import AgentContext, build_context
from taco_shared.db import AppDatabase
from taco_shared.paths import AGENT_SCENARIOS, load_json
from taco_shared.policy import load_policy


def load_scenarios(path: Path = AGENT_SCENARIOS) -> dict[str, Any]:
    return load_json(path)


def find_scenario(scenario_id: str, path: Path = AGENT_SCENARIOS) -> dict[str, Any]:
    for scenario in load_scenarios(path)["scenarios"]:
        if scenario["scenario_id"] == scenario_id:
            return scenario
    raise KeyError(f"unknown scenario {scenario_id!r}")


def context_for(intake: dict[str, Any], actor_id: str) -> AgentContext:
    return build_context(actor_id, intake["customer_id"], intake["case_id"], intake["complaint_date"])


def build_tools(settings: InferenceSettings, ctx: AgentContext, db: AppDatabase | None = None,
                cases: CaseApiClient | None = None) -> list[BaseTool]:
    return build_investigation_tools(ctx, db or AppDatabase(Path(settings.app_db)),
                                     cases or CaseApiClient(settings.case_api_url), load_policy(settings.policy_version))

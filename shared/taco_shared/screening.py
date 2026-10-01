"""Module 1 screening prompts: order, prior-resolution, and policy facts inlined in the prompt."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import DEFAULT_SCENARIOS, load_json


def load_scenarios(path: Path = DEFAULT_SCENARIOS) -> dict[str, Any]:
    return load_json(path)


def build_system_prompt(policy_text: str, schema: dict[str, Any]) -> str:
    return (
        "You are the complaint-resolution assistant for Taco Alley, a regional taco restaurant chain. "
        "You receive a customer complaint together with the verified order record, the customer's prior "
        "resolutions, and the current resolution policy. Every fact you need is included in the message; "
        "do not invent items, prices, dates, or prior history.\n\n"
        "Decide the correct resolution by applying the policy rules to the order record. Copy the order_id "
        "and item names exactly as they appear in the order record. Compute refund_amount from the prices "
        "on the order record as the rules direct.\n\n"
        "Respond with a single JSON object that conforms to this JSON Schema and nothing else: no prose, "
        "no markdown fences, no comments.\n\n"
        f"{json.dumps(schema, indent=2)}\n\n"
        f"POLICY\n{policy_text.strip()}"
    )


def build_user_prompt(scenario: dict[str, Any]) -> str:
    payload = {
        "complaint_date": scenario["complaint_date"],
        "customer_id": scenario["customer_id"],
        "complaint": scenario["complaint"],
        "order": scenario["order"],
        "prior_resolutions": scenario["prior_resolutions"],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def build_messages(scenario: dict[str, Any], system_prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": build_user_prompt(scenario)},
    ]

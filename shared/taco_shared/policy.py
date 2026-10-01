"""Versioned resolution policy: load, render as text, and select rules for a complaint category."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .paths import DEFAULT_POLICY_VERSION, load_json, policy_json_path, policy_text_path


@dataclass(frozen=True)
class Policy:
    version: str
    title: str
    definitions: tuple[str, ...]
    rules: tuple[dict[str, Any], ...]
    general_rule_ids: tuple[str, ...]
    closing: str

    @property
    def rule_ids(self) -> list[str]:
        return [rule["id"] for rule in self.rules]

    def rule(self, rule_id: str) -> dict[str, Any]:
        return next(rule for rule in self.rules if rule["id"] == rule_id)

    def rules_for_category(self, category: str) -> list[dict[str, Any]]:
        """Rules that apply to a complaint category, plus the general rules, in policy order."""
        return [rule for rule in self.rules
                if category in rule["categories"] or rule["id"] in self.general_rule_ids]

    def render_text(self) -> str:
        lines = [self.title, "", "Definitions"]
        lines += [f"- {definition}" for definition in self.definitions]
        lines += ["", "Rules"]
        lines += [f"{rule['id']:<3} {rule['text']}" for rule in self.rules]
        lines += ["", self.closing]
        return "\n".join(lines) + "\n"


@lru_cache(maxsize=None)
def load_policy(version: str = DEFAULT_POLICY_VERSION) -> Policy:
    data = load_json(policy_json_path(version))
    return Policy(
        version=data["version"], title=data["title"], definitions=tuple(data["definitions"]),
        rules=tuple(data["rules"]), general_rule_ids=tuple(data["general_rule_ids"]), closing=data["closing"],
    )


if __name__ == "__main__":
    policy = load_policy()
    policy_text_path(policy.version).write_text(policy.render_text(), encoding="utf-8", newline="\n")
    print(f"Wrote {policy_text_path(policy.version)}")

"""Inference connection settings from environment variables and a runtime-mounted secret file."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SECRET_FILE = "/run/secrets/inference_api_key"
PLACEHOLDER_KEY = "not-needed"


def _env(name: str, default: str) -> str:
    # Compose passes unset variables as empty strings.
    return os.environ.get(name) or default


def read_secret(path: str | Path) -> str | None:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


@dataclass(frozen=True)
class InferenceSettings:
    base_url: str
    model: str
    timeout_seconds: float = 120.0
    extra_body: dict[str, Any] = field(default_factory=dict)
    tool_calling: str = "native"
    structured_output: str = "json_schema"
    max_tool_calls: int = 8
    max_output_tokens: int = 800
    policy_version: str = "2026.09"
    actor_id: str = "rep-kop-01"
    case_api_url: str = "http://case-api:8081"
    app_db: str = "/data/app/taco_app.db"
    results_dir: str = "results"
    api_key: str = field(default=PLACEHOLDER_KEY, repr=False)
    api_key_source: str = "placeholder"

    @classmethod
    def from_env(cls) -> InferenceSettings:
        secret_file = _env("INFERENCE_API_KEY_FILE", DEFAULT_SECRET_FILE)
        key = read_secret(secret_file)
        extra = _env("INFERENCE_EXTRA_BODY", "{}")
        extra_body = json.loads(extra)
        if not isinstance(extra_body, dict):
            raise ValueError("INFERENCE_EXTRA_BODY must be a JSON object")
        return cls(
            base_url=_env("INFERENCE_BASE_URL", "http://localhost:8080/v1").rstrip("/"),
            model=_env("INFERENCE_MODEL", ""),
            timeout_seconds=float(_env("INFERENCE_TIMEOUT_SECONDS", "120")),
            extra_body=extra_body,
            tool_calling=_env("INFERENCE_TOOL_CALLING", "native"),
            structured_output=_env("INFERENCE_STRUCTURED_OUTPUT", "json_schema"),
            max_tool_calls=int(_env("AGENT_MAX_TOOL_CALLS", "8")),
            max_output_tokens=int(_env("AGENT_MAX_OUTPUT_TOKENS", "800")),
            policy_version=_env("TACO_POLICY_VERSION", "2026.09"),
            actor_id=_env("TACO_ACTOR", "rep-kop-01"),
            case_api_url=_env("CASE_API_URL", "http://case-api:8081"),
            app_db=_env("TACO_APP_DB", "/data/app/taco_app.db"),
            results_dir=_env("RESULTS_DIR", "results"),
            api_key=key or PLACEHOLDER_KEY,
            api_key_source=f"secret file {secret_file}" if key else "placeholder (no secret file)",
        )

    def describe(self) -> dict[str, Any]:
        """Settings safe to print or record; the key itself is never included."""
        return {"base_url": self.base_url, "model": self.model, "timeout_seconds": self.timeout_seconds,
                "extra_body": self.extra_body, "tool_calling": self.tool_calling,
                "structured_output": self.structured_output, "max_tool_calls": self.max_tool_calls,
                "max_output_tokens": self.max_output_tokens, "policy_version": self.policy_version,
                "api_key_source": self.api_key_source}

"""Trace export policy for LangSmith (module 6, planning decision 28).

TRACE_CONTENT_POLICY:
  full      export inputs and outputs as they are (modules 4-5 behaviour, the default)
  redacted  keep structure, ids, tool names, and short codes; replace free text and personal fields
  none      hide all inputs and outputs; runs keep names, timings, tokens, and metadata
"""

from __future__ import annotations

import os
import re
from typing import Any

REDACTED = "[redacted]"
# Values under these keys are personal or free text even when short.
SENSITIVE_KEY = re.compile(r"(?i)(customer_name|first_name|last_name|full_name|email|phone|address|complaint|"
                           r"message|note|answer|justification|content|text)")
# Short identifiers and codes survive: case and order ids, tool names, categories, rule ids, statuses.
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/#-]{1,40}$")


def redact(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, key) for item in value]
    if isinstance(value, str):
        if key and SENSITIVE_KEY.search(key):
            return REDACTED if value else value
        return value if SAFE_VALUE.match(value) else REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # Messages and other objects (tool results arrive as ToolMessage): redact their serialized form.
    try:
        from langchain_core.load import dumpd
        return redact(dumpd(value), key)
    except Exception:  # noqa: BLE001 - anything that cannot be serialized is not exported
        return REDACTED


def policy() -> str:
    value = (os.environ.get("TRACE_CONTENT_POLICY") or "full").lower()
    return value if value in {"full", "redacted", "none"} else "redacted"


def configure_tracing() -> str:
    """Install the LangSmith client used by every LangChain tracer, following the policy. Returns the policy."""
    chosen = policy()
    if chosen == "full" or os.environ.get("LANGSMITH_TRACING", "false").lower() != "true":
        return chosen
    from langsmith import run_trees

    hide = True if chosen == "none" else redact
    run_trees.get_cached_client(hide_inputs=hide, hide_outputs=hide)
    return chosen

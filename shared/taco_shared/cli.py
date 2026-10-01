"""Command-line connection flags shared by the module tools."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any


def add_connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BASE_URL") or "http://localhost:8080/v1",
        help="OpenAI-compatible base URL ending in /v1 (default: $BASE_URL or http://localhost:8080/v1).",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY") or "not-needed",
        help="API key; module 1 serves without authentication (default: $OPENAI_API_KEY or not-needed).",
    )
    parser.add_argument(
        "--model",
        # Compose passes unset variables as "", which must still trigger model discovery.
        default=os.environ.get("MODEL") or None,
        help="Served model name (default: $MODEL, or the first model returned by /v1/models).",
    )
    parser.add_argument(
        "--metrics-url",
        default=os.environ.get("METRICS_URL") or None,
        help="Prometheus metrics URL (default: derived from --base-url by replacing /v1 with /metrics).",
    )
    parser.add_argument(
        "--extra-body",
        default=os.environ.get("EXTRA_BODY"),
        help='JSON merged into every chat request, e.g. \'{"chat_template_kwargs": {"enable_thinking": false}}\'.',
    )
    parser.add_argument("--request-timeout", type=float, default=300.0, help="Per-request timeout in seconds.")


def parse_extra_body(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("--extra-body must be a JSON object.")
    return value

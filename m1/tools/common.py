"""Shared helpers for the module 1 demo scripts."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

M1_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = M1_ROOT / "scenarios"
DEFAULT_SCENARIOS = SCENARIOS_DIR / "screening_scenarios.json"
DEFAULT_SCHEMA = SCENARIOS_DIR / "proposal_schema.json"
DEFAULT_POLICY = SCENARIOS_DIR / "policy.txt"
DEFAULT_RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "results"))

METRIC_NAMES = (
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:gpu_cache_usage_perc",
    "vllm:kv_cache_usage_perc",
    "vllm:num_preemptions_total",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "vllm:request_success_total",
)


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


def metrics_url_from_base(base_url: str, metrics_url: str | None) -> str:
    if metrics_url:
        return metrics_url
    return re.sub(r"/v1/?$", "", base_url.rstrip("/")) + "/metrics"


def fetch_metrics(url: str, timeout: float = 5.0) -> dict[str, float] | None:
    """Return a flat snapshot of the vLLM metrics we care about, summing over label sets."""
    try:
        response = httpx.get(url, timeout=timeout)
        response.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return None
    snapshot: dict[str, float] = {}
    for line in response.text.splitlines():
        if not line or line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if name not in METRIC_NAMES:
            continue
        try:
            value = float(line.rsplit(" ", 1)[-1])
        except ValueError:
            continue
        snapshot[name] = snapshot.get(name, 0.0) + value
    return snapshot or None


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


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


def strict_json_loads(text: str) -> Any:
    def reject_constant(value: str) -> Any:
        raise ValueError(f"Invalid JSON constant: {value}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=reject_duplicates)


def parse_json_output(text: str) -> tuple[Any, bool]:
    """Return (parsed, strict). strict is False when JSON had to be recovered from surrounding text."""
    cleaned = text.strip()
    # Some reasoning models leak <think> blocks into content when no reasoning parser is configured.
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
    try:
        return strict_json_loads(cleaned), True
    except ValueError:
        pass
    if cleaned.startswith("```"):
        cleaned = "\n".join(line for line in cleaned.splitlines() if not line.strip().startswith("```"))
    try:
        return strict_json_loads(cleaned), False
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("No JSON object found in model output.") from None
        return strict_json_loads(cleaned[start : end + 1]), False


def describe_error(error: Exception) -> dict[str, Any]:
    """Compact, credential-free description of an API or transport error."""
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    message = None
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict):
            message = inner.get("message") or inner.get("detail")
        else:
            message = str(inner)
    if not message:
        message = str(error)
    message = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", " ".join(str(message).split()))[:1500]
    kind = "compatibility" if status in {400, 404, 405, 422} else "auth" if status in {401, 403} else \
        "rate_limit" if status == 429 else "server" if status and status >= 500 else "transport"
    return {"type": type(error).__name__, "status_code": status, "kind": kind, "message": message}


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def summarize_latency(values: list[float]) -> dict[str, float | None]:
    return {
        "mean": statistics.mean(values) if values else None,
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values) if values else None,
    }


def peak_vram_from_log(path: Path | None) -> dict[str, Any] | None:
    """Read the CSV written by tools/sample_vram.sh and report the peak memory.used value."""
    if path is None or not path.is_file():
        return None
    peak_used = peak_util = 0.0
    total = None
    samples = 0
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            used, total_value, util = float(parts[1]), float(parts[2]), float(parts[3])
        except ValueError:
            continue
        samples += 1
        peak_used = max(peak_used, used)
        peak_util = max(peak_util, util)
        total = total_value
    if not samples:
        return None
    return {"samples": samples, "peak_memory_used_mib": peak_used, "memory_total_mib": total,
            "peak_gpu_utilization_pct": peak_util, "source": str(path)}


def timestamp_slug() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "model"


def print_table(headers: list[str], rows: list[list[Any]]) -> None:
    def fmt(cell: Any) -> str:
        if cell is None:
            return "-"
        if isinstance(cell, float):
            return f"{cell:.2f}"
        return str(cell)

    text_rows = [[fmt(cell) for cell in row] for row in rows]
    widths = [max(len(headers[i]), *(len(row[i]) for row in text_rows)) if text_rows else len(headers[i])
              for i in range(len(headers))]
    line = "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers))
    print(line)
    print("  ".join("-" * width for width in widths))
    for row in text_rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))

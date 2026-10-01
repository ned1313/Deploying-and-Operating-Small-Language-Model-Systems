"""vLLM /metrics scraping."""

from __future__ import annotations

import re

import httpx

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

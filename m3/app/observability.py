"""Request ids, JSON logs, Prometheus metrics, and deployment versions (connected to monitoring in module 4).

Logs carry identifiers, timings, counts, and outcomes, never complaint text or proposal content.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from taco_shared.agent import prompts
from taco_shared.paths import AGENT_SCENARIOS_V2, load_json

REQUEST_ID = contextvars.ContextVar("request_id", default="-")
VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

HTTP_REQUESTS = Counter("taco_http_requests_total", "Complaint API requests", ["method", "route", "status"])
HTTP_SECONDS = Histogram("taco_http_request_seconds", "Complaint API request latency", ["route"],
                         buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 90))
RUNS = Counter("taco_workflow_runs_total", "Workflow invocations by resulting status", ["status"])
RUN_SECONDS = Histogram("taco_workflow_run_seconds", "Workflow invocation latency (excludes approval wait)",
                        buckets=(0.5, 1, 2.5, 5, 7.5, 10, 15, 20, 30, 60, 90))
NODE_SECONDS = Histogram("taco_workflow_node_seconds", "Graph node latency", ["node"],
                         buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16))
TOOL_CALLS = Counter("taco_tool_calls_total", "Investigation tool calls", ["tool", "status", "error_kind"])
TOOL_SECONDS = Histogram("taco_tool_seconds", "Investigation tool latency", ["tool"],
                         buckets=(0.005, 0.01, 0.05, 0.1, 0.5, 1, 3, 10))
CASE_API_RETRIES = Counter("taco_case_api_retries_total", "Case API retries made by tools")
MODEL_CALLS = Counter("taco_model_calls_total", "Model calls", ["node"])
MODEL_SECONDS = Histogram("taco_model_seconds", "Model call latency as seen by the application", ["node"],
                          buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32))
MODEL_TOKENS = Counter("taco_model_tokens_total", "Model tokens", ["node", "direction"])
APPROVAL_WAIT = Histogram("taco_approval_wait_seconds", "Time a proposal waited for a reviewer decision",
                          buckets=(5, 15, 30, 60, 300, 900, 3600, 14400, 86400))
RUNS_IN_PROGRESS = Gauge("taco_workflow_runs_in_progress", "Workflow runs holding a run slot")
QUEUE_WAIT = Histogram("taco_workflow_queue_wait_seconds", "Time a request waited for a workflow run slot",
                       buckets=(0.01, 0.1, 0.5, 1, 2.5, 5, 10, 20, 40, 60))
QUEUE_REJECTED = Counter("taco_workflow_queue_rejected_total", "Requests rejected because no run slot freed up")
BUILD_INFO = Gauge("taco_build_info", "Deployment versions of the complaint API (value is always 1)",
                   ["app_version", "model", "policy_version", "prompt_version", "schema_version", "dataset_version"])

# Create the series alerts depend on at 0: rate() cannot see a counter's first increment from nothing.
for _tool in ("get_order", "get_prior_resolutions", "get_resolution_policy", "get_case_status"):
    for _status, _kind in (("ok", "none"), ("error", "unavailable"), ("error", "not_found")):
        TOOL_CALLS.labels(_tool, _status, _kind)
for _status in ("pending_review", "needs_clarification", "escalated", "recorded", "rejected", "failed_retryable"):
    RUNS.labels(_status)


def deployment_versions(model: str | None, policy_version: str | None) -> dict[str, str]:
    """Versions that pin a result: tagged on metrics, logs, and traces."""
    prompt_text = "\n".join(value for name, value in sorted(vars(prompts).items())
                            if name.isupper() and isinstance(value, str))
    return {"app_version": os.environ.get("TACO_APP_VERSION") or "dev", "model": model or "unknown",
            "policy_version": policy_version or "unknown",
            "prompt_version": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:8],
            "schema_version": "proposal.v2",
            "dataset_version": load_json(AGENT_SCENARIOS_V2).get("dataset_version", "unknown")}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
                                 "level": record.levelname, "logger": record.name, "event": record.getMessage(),
                                 "request_id": REQUEST_ID.get()}
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, default=str)


def configure_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("taco")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


LOG = configure_logging()


def log(event: str, **fields: Any) -> None:
    LOG.info(event, extra={"fields": fields})


def new_request_id(candidate: str | None) -> str:
    return candidate if candidate and VALID_REQUEST_ID.match(candidate) else uuid.uuid4().hex


def current_request_id() -> str | None:
    value = REQUEST_ID.get()
    return None if value == "-" else value


def observe(event: str, data: dict[str, Any]) -> None:
    """Workflow observer: turns node, tool, and model events into metrics and content-free log lines."""
    if event == "node":
        NODE_SECONDS.labels(data["node"]).observe(data["seconds"])
    elif event == "tool":
        TOOL_CALLS.labels(data["tool"], data["status"] or "error", data.get("error_kind") or "none").inc()
        TOOL_SECONDS.labels(data["tool"]).observe(data["seconds"])
        if data.get("retries"):
            CASE_API_RETRIES.inc(data["retries"])
        log("tool_call", tool=data["tool"], status=data["status"], error_kind=data.get("error_kind"),
            retries=data.get("retries", 0), ms=round(data["seconds"] * 1000, 1))
    elif event == "model":
        MODEL_CALLS.labels(data["node"]).inc()
        MODEL_SECONDS.labels(data["node"]).observe(data["seconds"])
        for direction in ("input", "output"):
            if data.get(f"{direction}_tokens"):
                MODEL_TOKENS.labels(data["node"], direction).inc(data[f"{direction}_tokens"])
        log("model_call", node=data["node"], ms=round(data["seconds"] * 1000, 1),
            input_tokens=data.get("input_tokens"), output_tokens=data.get("output_tokens"))

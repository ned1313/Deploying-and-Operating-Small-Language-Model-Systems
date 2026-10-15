"""Which workflow outcomes are sampled for quality evaluation, and what a sample contains (decisions 3 and 5).

A sample is the content needed for scoring: the complaint, the evidence the tools returned, the final
proposal and its validation, counters, tokens, any recorded action, ids, and deployment versions. No
message history (LangSmith has it when tracing is on).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

FINAL_STATUSES = {"pending_review", "recorded", "escalated", "needs_clarification", "rejected", "failed_retryable"}
ALWAYS_SAMPLE_STATUSES = {"escalated", "needs_clarification", "rejected"}


def in_sample(case_id: str, rate: float) -> bool:
    """Deterministic per case, so replaying the same workload samples the same cases."""
    if rate >= 1:
        return True
    if rate <= 0:
        return False
    bucket = int(hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < rate


def sample_reason(case_id: str, status: str, corrections: int, rate: float) -> str | None:
    """Why this outcome is sampled, or None. Escalations, clarifications, rejections, and corrected
    proposals are always sampled while sampling is on (rate > 0)."""
    if rate <= 0 or status not in FINAL_STATUSES:
        return None
    if status in ALWAYS_SAMPLE_STATUSES:
        return f"always:{status}"
    if corrections:
        return "always:corrected"
    return "rate" if in_sample(case_id, rate) else None


def _parse(content: Any) -> Any:
    if isinstance(content, str):
        try:
            return json.loads(content)
        except ValueError:
            return content
    return content


def build_record(case: dict[str, Any], values: dict[str, Any], versions: dict[str, str],
                 run_ids: list[str], trace_url: str | None) -> dict[str, Any]:
    steps = values.get("steps", [])
    actions: dict[str, Any] = {}
    for step in steps:
        action = step.get("action") if step.get("kind") == "record" else None
        if action and action.get("action_id"):
            actions[action["action_id"]] = action
    tokens = {"input": sum(s.get("input_tokens") or 0 for s in steps),
              "output": sum(s.get("output_tokens") or 0 for s in steps)}
    return {
        "sample_id": case["thread_id"], "case_id": case["case_id"], "thread_id": case["thread_id"],
        "request_id": case.get("last_request_id"), "run_ids": run_ids, "trace_url": trace_url,
        "actor_id": case["submitted_by"], "store_id": case["store_id"], "scenario_id": case.get("scenario_id"),
        "status": case["status"], "outcome_reason": case.get("outcome_reason"), "versions": versions,
        "intake": values.get("intake", {}), "complaint": values.get("intake", {}).get("complaint"),
        "evidence": [{"tool": e["tool"], "args": e.get("args"), "result": _parse(e["content"])}
                     for e in values.get("evidence_log", []) if e.get("executed")],
        "proposal": values.get("proposal"), "proposal_version": values.get("proposal_version", 0),
        "validation": values.get("validation"), "reviews": values.get("reviews", []),
        "actions": list(actions.values()),
        "counters": {"tool_calls": values.get("tool_calls_used", 0), "repeated_calls": values.get("repeated_calls", 0),
                     "nudges": values.get("nudges_used", 0), "corrections": values.get("corrections_used", 0),
                     "model_calls": sum(1 for s in steps if s.get("kind") in {"model", "finalize"})},
        "tokens": tokens,
    }

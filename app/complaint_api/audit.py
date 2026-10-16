"""Audit capture for the complaint API (module 6).

Events come from three places: the service records intake, review decisions, clarifications, and admin
actions as they happen; after every workflow run, `after_run` turns the run's new workflow steps into
`model_call`, `tool_access`, `proposal_validated`, and `action_recorded` events (a per-thread cursor marks
what has been recorded); the API records denied requests, audit reads, and deletions.

Only the final model response is captured for each model call; full prompts are in LangSmith traces.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from taco_shared.audit import AuditStore, load_key

from .observability import log


def digest(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class AuditRecorder:
    def __init__(self, store: AuditStore) -> None:
        self.store = store

    @classmethod
    def from_env(cls) -> AuditRecorder | None:
        """On when AUDIT_KEY_FILE holds a Fernet key; off (None) otherwise, as in modules 2-5."""
        key = load_key(os.environ.get("AUDIT_KEY_FILE"))
        if key is None:
            return None
        path = Path(os.environ.get("TACO_AUDIT_DB") or "/data/audit/audit.db")
        return cls(AuditStore(path, key, service=os.environ.get("AUDIT_SERVICE_ID") or "complaint-api"))

    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        return self.store.append(kind, **fields)

    # Workflow runs ---------------------------------------------------------------------------------

    def after_run(self, case: dict[str, Any], values: dict[str, Any], run_name: str, status: str, reason: str | None,
                  request_id: str, actor_id: str, role: str | None, versions: dict[str, str]) -> None:
        """Record the run's new steps, then the run outcome. Logged, never raised: the run already happened."""
        try:
            self._steps(case, values, request_id, actor_id, role, versions)
            self.record("workflow_run", case_id=case["case_id"], request_id=request_id, thread_id=case["thread_id"],
                        actor_id=actor_id, role=role,
                        metadata={"run": run_name, "status": status, "reason": reason, **versions})
        except Exception as error:  # noqa: BLE001 - reported, and visible as a gap in the case's events
            log("audit_write_failed", case_id=case["case_id"], error=type(error).__name__)

    def _steps(self, case: dict[str, Any], values: dict[str, Any], request_id: str, actor_id: str,
               role: str | None, versions: dict[str, str]) -> None:
        thread_id = case["thread_id"]
        steps, evidence = values.get("steps", []), values.get("evidence_log", [])
        seen_steps, evidence_index = self.store.cursor(thread_id)
        common = {"case_id": case["case_id"], "request_id": request_id, "thread_id": thread_id,
                  "actor_id": actor_id, "role": role}
        proposal, current_version = values.get("proposal"), values.get("proposal_version")
        for step in steps[seen_steps:]:
            kind = step.get("kind")
            if kind == "tool":
                entry = evidence[evidence_index] if evidence_index < len(evidence) else {}
                evidence_index += 1
                self.record("tool_access", **common,
                            metadata={"tool": step.get("name"), "args": entry.get("args"), "status": step.get("status"),
                                      "error_kind": step.get("error_kind"), "record_id": step.get("record_id"),
                                      "executed": step.get("executed"), "retries": step.get("retries")},
                            content={"result": entry.get("content")})
            elif kind == "model":
                self.record("model_call", **common,
                            metadata={"node": step.get("node"), "model": versions.get("model"),
                                      "finish_reason": step.get("finish_reason"),
                                      "input_tokens": step.get("input_tokens"), "output_tokens": step.get("output_tokens"),
                                      "seconds": step.get("elapsed_seconds"),
                                      "tool_calls": [c.get("name") for c in step.get("tool_calls", [])],
                                      "injected_fault": step.get("injected_fault")},
                            content={"content": step.get("content"), "tool_calls": step.get("tool_calls")})
            elif kind == "finalize":
                same = step.get("proposal_version") == current_version
                self.record("model_call", **common,
                            metadata={"node": "propose", "model": versions.get("model"),
                                      "proposal_version": step.get("proposal_version"),
                                      "finish_reason": step.get("finish_reason"), "error": step.get("error"),
                                      "input_tokens": step.get("input_tokens"), "output_tokens": step.get("output_tokens"),
                                      "seconds": step.get("elapsed_seconds"), "injected_fault": step.get("injected_fault")},
                            content={"proposal": proposal if same else None,
                                     "raw": values.get("proposal_raw") if same else None})
            elif kind == "validate":
                same = step.get("proposal_version") == current_version
                self.record("proposal_validated", **common,
                            metadata={"proposal_version": step.get("proposal_version"), "accepted": step.get("accepted"),
                                      "schema_errors": step.get("schema_errors", []),
                                      "business_rule_violations": step.get("business_rule_violations", []),
                                      "proposal_sha256": digest(proposal) if same else None},
                            content={"proposal": proposal} if same else None)
            elif kind == "record":
                action = step.get("action") or {}
                self.record("action_recorded" if not step.get("conflict") else "action_refused", **common,
                            metadata={"idempotency_key": step.get("idempotency_key"), "action_id": action.get("action_id"),
                                      "type": action.get("type"), "amount": action.get("amount"),
                                      "replayed": step.get("replayed"), "conflict": step.get("conflict"),
                                      "retries": step.get("retries")},
                            content={"action": action} if action else None)
            elif kind in {"clarify", "escalate", "rejected", "nudge"}:
                self.record(f"workflow_{kind}", **common,
                            metadata={"reason": step.get("reason"), "missing": step.get("missing")})
        self.store.set_cursor(thread_id, len(steps), evidence_index)

"""Complaint service: authorization, case records, and runs of the LangGraph workflow.

One worker process runs up to `max_concurrent_runs` graph runs at once; further requests wait for a
slot up to `queue_timeout_seconds`, then get 503. Operations on the same case are serialized. A run
either pauses at the approval interrupt, ends (clarify, escalate, rejected, recorded), or fails on an
unavailable dependency and keeps its checkpoint so `retry` resumes from the failed step.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import HTTPException
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from taco_shared.agent.faults import InferenceUnavailable
from taco_shared.case_api_client import CaseApiClient, CaseApiUnavailable
from taco_shared.context import Actor
from taco_shared.quality.sampling import build_record, sample_reason
from taco_shared.quality.store import QualityStore
from taco_shared.workflow import WorkflowDeps, build_graph, case_status

from .observability import (APPROVAL_WAIT, QUEUE_REJECTED, QUEUE_WAIT, RUN_SECONDS, RUNS, RUNS_IN_PROGRESS,
                            log, trace_url)
from .store import AppStore, now

RESET_COUNTERS = {"tool_calls_used": 0, "repeated_calls": 0, "nudges_used": 0, "corrections_used": 0,
                  "next_step": None, "outcome_reason": None, "clarification_question": None}


@dataclass(frozen=True)
class ServiceSettings:
    state_dir: Path
    run_timeout_seconds: float = 90.0
    recursion_limit: int = 100
    max_concurrent_runs: int = 1
    queue_timeout_seconds: float = 60.0
    versions: dict[str, str] = field(default_factory=dict)
    # Called per run so a model swapped behind the stable alias shows up without a restart.
    versions_provider: Callable[[], dict[str, str]] | None = None
    quality_db: Path | None = None
    quality_sample_rate: float = 0.0


class ComplaintService:
    def __init__(self, deps: WorkflowDeps, cases: CaseApiClient, settings: ServiceSettings) -> None:
        self.deps, self.cases, self.settings = deps, cases, settings
        self.store = AppStore(settings.state_dir / "app.db")
        self.saver = SqliteSaver(sqlite3.connect(settings.state_dir / "checkpoints.db", check_same_thread=False))
        self.graph = build_graph(deps).compile(checkpointer=self.saver)
        self._slots = threading.BoundedSemaphore(max(1, settings.max_concurrent_runs))
        self._case_locks: dict[str, threading.Lock] = {}
        self._case_locks_guard = threading.Lock()
        self.quality = (QualityStore(settings.quality_db)
                        if settings.quality_db is not None and settings.quality_sample_rate > 0 else None)

    def versions(self) -> dict[str, str]:
        return self.settings.versions_provider() if self.settings.versions_provider else self.settings.versions

    # Runs -------------------------------------------------------------------------------------

    @contextmanager
    def _slot(self, case_id: str, blocking: bool = False) -> Iterator[None]:
        """Hold a run slot and the case's lock; 503 when no slot frees up within the queue timeout."""
        started = time.perf_counter()
        if not self._slots.acquire(timeout=None if blocking else self.settings.queue_timeout_seconds):
            QUEUE_REJECTED.inc()
            log("workflow_queue_rejected", case_id=case_id)
            raise HTTPException(status_code=503, detail="all workflow run slots are busy; retry later",
                                headers={"Retry-After": "10"})
        QUEUE_WAIT.observe(time.perf_counter() - started)
        RUNS_IN_PROGRESS.inc()
        try:
            with self._case_locks_guard:
                lock = self._case_locks.setdefault(case_id, threading.Lock())
            with lock:
                yield
        finally:
            RUNS_IN_PROGRESS.dec()
            self._slots.release()

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id, "deadline": time.time() + self.settings.run_timeout_seconds},
                "recursion_limit": self.settings.recursion_limit}

    def _run_config(self, case: dict[str, Any], run_name: str, request_id: str, actor_id: str,
                    run_id: str | None = None) -> dict[str, Any]:
        """Graph config plus the run id, name, tags, and metadata that LangSmith records (when tracing is on)."""
        tags = [run_name, *(f"injected_fault:{fault['mode']}" for fault in self.deps.faults.describe())]
        if case.get("scenario_id"):
            tags.append(f"scenario:{case['scenario_id']}")
        metadata = {"request_id": request_id, "case_id": case["case_id"], "thread_id": case["thread_id"],
                    "store_id": case["store_id"], "actor_id": actor_id, **self.versions()}
        config = {**self._config(case["thread_id"]), "run_name": run_name, "tags": tags, "metadata": metadata}
        if run_id:
            config["run_id"] = uuid.UUID(run_id)
        return config

    def _run(self, case: dict[str, Any], payload: Any, request_id: str, run_name: str, actor_id: str) -> None:
        started = time.perf_counter()
        failure: tuple[str, str] | None = None
        run_id = str(uuid.uuid4())
        try:
            self.graph.invoke(payload, self._run_config(case, run_name, request_id, actor_id, run_id))
        except InferenceUnavailable as error:
            failure = ("failed_retryable", f"inference_unavailable: {error}")
        except CaseApiUnavailable as error:
            failure = ("failed_retryable", f"case_api_unavailable: {error}")
        except GraphRecursionError:
            failure = ("escalated", "step_limit")
        seconds = time.perf_counter() - started
        if failure:
            self.store.update(case["case_id"], status=failure[0], outcome_reason=failure[1], last_request_id=request_id)
        else:
            self._sync(case["case_id"], request_id)
        status = self.store.get(case["case_id"])["status"]
        RUNS.labels(status).inc()
        RUN_SECONDS.observe(seconds)
        log("workflow_run", case_id=case["case_id"], thread_id=case["thread_id"], run=run_name, status=status,
            reason=failure[1] if failure else None, seconds=round(seconds, 3))
        self._sample(case["case_id"], run_id)

    def _sample(self, case_id: str, run_id: str) -> None:
        """Write or update the case's quality sample. Never fails the request."""
        if self.quality is None:
            return
        try:
            case = self.store.get(case_id)
            values = self.graph.get_state(self._config(case["thread_id"])).values
            existing = self.quality.sample(case["thread_id"])
            if existing is not None and existing["sample_id"] != case["thread_id"]:
                existing = None
            reason = (existing or {}).get("sampled_reason") or sample_reason(
                case_id, case["status"], values.get("corrections_used", 0), self.settings.quality_sample_rate)
            if reason is None:
                return
            run_ids = [*(existing or {}).get("run_ids", []), run_id]
            record = build_record(case, values, self.versions(), run_ids,
                                  (existing or {}).get("trace_url") or trace_url(run_ids[0]))
            self.quality.upsert_sample(record, reason)
            log("quality_sample", case_id=case_id, sample_id=record["sample_id"], status=case["status"], reason=reason)
        except Exception as error:  # noqa: BLE001 - sampling is best effort by design
            log("quality_sample_failed", case_id=case_id, error=type(error).__name__)

    def _sync(self, case_id: str, request_id: str | None = None) -> None:
        case = self.store.get(case_id)
        snapshot = self.graph.get_state(self._config(case["thread_id"]))
        values = snapshot.values
        status = case_status(values, bool(snapshot.interrupts))
        fields: dict[str, Any] = {"status": status, "outcome_reason": values.get("outcome_reason"),
                                  "proposal_version": values.get("proposal_version", 0), "action": values.get("action")}
        if status == "pending_review" and case["status"] != "pending_review":
            fields["review_requested_at"] = now()
        if request_id:
            fields["last_request_id"] = request_id
        self.store.update(case_id, **fields)

    def resume_incomplete(self) -> int:
        """Resume runs that were interrupted mid-investigation (for example, the container was killed)."""
        pending = self.store.by_status("investigating") + self.store.by_status("approved")
        for case in pending:
            if self.store.get(case["case_id"])["status"] not in {"investigating", "approved"}:
                continue
            log("resume_incomplete_run", case_id=case["case_id"], thread_id=case["thread_id"])
            with self._slot(case["case_id"], blocking=True):
                self._run(case, None, "startup-resume", "resume", "system")
        return len(pending)

    # Authorization helpers -----------------------------------------------------------------

    def _visible(self, actor: Actor, case_id: str) -> dict[str, Any]:
        case = self.store.get(case_id)
        if case is None or case["store_id"] not in actor.store_ids:
            raise HTTPException(status_code=404, detail="case not found")
        return case

    # Operations ---------------------------------------------------------------------------------

    def submit(self, actor: Actor, intake: dict[str, Any], request_id: str,
               scenario_id: str | None = None) -> tuple[dict[str, Any], bool]:
        if not actor.can_submit:
            raise HTTPException(status_code=403, detail="only service representatives submit complaints")
        try:
            external = self.cases.get_case(intake["case_id"], actor.store_ids)
        except CaseApiUnavailable as error:
            raise HTTPException(status_code=503, detail=f"case API unavailable: {error}") from error
        if external is None:
            raise HTTPException(status_code=404, detail="case not found")
        if external["customer_id"] != intake["customer_id"]:
            raise HTTPException(status_code=422, detail="customer_id does not match the case")
        with self._slot(intake["case_id"]):
            existing = self.store.get(intake["case_id"])
            if existing is not None:
                return self.view(existing["case_id"]), False
            thread_id = f"{intake['case_id']}:{uuid.uuid4().hex[:8]}"
            self.store.create(intake["case_id"], thread_id, external["store_id"], intake["customer_id"], actor.actor_id,
                              scenario_id, request_id)
            payload = {"request_id": request_id, "actor_id": actor.actor_id, "store_ids": list(actor.store_ids),
                       "intake": intake, "proposal_version": 0, "status": "investigating", **RESET_COUNTERS}
            self._run(self.store.get(intake["case_id"]), payload, request_id, "submit", actor.actor_id)
            return self.view(intake["case_id"]), True

    def decide(self, actor: Actor, case_id: str, decision: str, proposal_version: int, note: str | None,
               request_id: str) -> tuple[dict[str, Any], bool]:
        self._visible(actor, case_id)
        with self._slot(case_id):
            return self._decide(actor, case_id, decision, proposal_version, note, request_id)

    def _decide(self, actor: Actor, case_id: str, decision: str, proposal_version: int, note: str | None,
                request_id: str) -> tuple[dict[str, Any], bool]:
        case = self._visible(actor, case_id)
        if decision not in {"approve", "reject", "revise"}:
            raise HTTPException(status_code=422, detail="decision must be approve, reject, or revise")
        if not actor.can_review(case["store_id"]):
            raise HTTPException(status_code=403, detail="only a reviewer for this store or an area manager may decide")
        if actor.actor_id == case["submitted_by"]:
            raise HTTPException(status_code=403, detail="a reviewer cannot decide on a case they submitted")
        if decision == "revise" and not (note or "").strip():
            raise HTTPException(status_code=422, detail="a revision request needs a note")
        last = (self.store.reviews(case_id) or [None])[-1]
        if (last and last["decision"] == decision and last["proposal_version"] == proposal_version
                and case["status"] in {"recorded", "rejected"}):
            return self.view(case_id), False
        if case["status"] != "pending_review":
            raise HTTPException(status_code=409, detail=f"case is {case['status']}, not pending_review")
        if proposal_version != case["proposal_version"]:
            raise HTTPException(status_code=409, detail=f"proposal version {proposal_version} is stale; "
                                                        f"current version is {case['proposal_version']}")
        at = self.store.add_review(case_id, proposal_version, decision, actor.actor_id, note)
        if case.get("review_requested_at"):
            waited = (datetime.fromisoformat(at) - datetime.fromisoformat(case["review_requested_at"])).total_seconds()
            APPROVAL_WAIT.observe(max(waited, 0.0))
        log("review_decision", case_id=case_id, decision=decision, reviewer=actor.actor_id, proposal_version=proposal_version)
        self.store.update(case_id, status={"approve": "approved", "reject": "rejected"}.get(decision, "investigating"),
                          review_requested_at=None)
        self._run(case, Command(resume={"decision": decision, "reviewer": actor.actor_id, "note": note, "at": at}),
                  request_id, "decision", actor.actor_id)
        return self.view(case_id), True

    def clarify(self, actor: Actor, case_id: str, answer: str, request_id: str) -> dict[str, Any]:
        self._visible(actor, case_id)
        with self._slot(case_id):
            case = self._visible(actor, case_id)
            if not actor.can_submit:
                raise HTTPException(status_code=403, detail="only service representatives add clarifications")
            if case["status"] != "needs_clarification":
                raise HTTPException(status_code=409, detail=f"case is {case['status']}, not needs_clarification")
            values = self.graph.get_state(self._config(case["thread_id"])).values
            intake = dict(values["intake"])
            intake["complaint"] = f"{intake['complaint']}\nCustomer clarification: {answer.strip()}"
            self.store.update(case_id, status="investigating", outcome_reason=None)
            self._run(case, {"intake": intake, "status": "investigating", **RESET_COUNTERS}, request_id, "clarify",
                      actor.actor_id)
            return self.view(case_id)

    def retry(self, actor: Actor, case_id: str, request_id: str) -> dict[str, Any]:
        self._visible(actor, case_id)
        with self._slot(case_id):
            case = self._visible(actor, case_id)
            if case["status"] != "failed_retryable":
                raise HTTPException(status_code=409, detail=f"case is {case['status']}, not failed_retryable")
            self.store.update(case_id, status="investigating", outcome_reason=None)
            self._run(case, None, request_id, "retry", actor.actor_id)
            return self.view(case_id)

    def reset(self, case_ids: list[str] | None) -> int:
        targets = case_ids if case_ids is not None else self.store.case_ids()
        removed = 0
        for case_id in targets:
            thread_id = self.store.delete(case_id)
            if thread_id:
                self.saver.delete_thread(thread_id)
                removed += 1
        return removed

    # Views -------------------------------------------------------------------------------------

    def view(self, case_id: str, include_steps: bool = False) -> dict[str, Any]:
        case = self.store.get(case_id)
        values = self.graph.get_state(self._config(case["thread_id"])).values
        steps = values.get("steps", [])
        evidence_log = values.get("evidence_log", [])
        result = {
            "case_id": case_id, "status": case["status"], "outcome_reason": case["outcome_reason"],
            "store_id": case["store_id"], "customer_id": case["customer_id"], "submitted_by": case["submitted_by"],
            "scenario_id": case["scenario_id"], "thread_id": case["thread_id"], "request_id": case["last_request_id"],
            "complaint": values.get("intake", {}).get("complaint"),
            "clarification_question": values.get("clarification_question"),
            "proposal_version": case["proposal_version"], "proposal": values.get("proposal"),
            "validation": values.get("validation"), "reviews": self.store.reviews(case_id), "action": case["action"],
            "counters": {"tool_calls": values.get("tool_calls_used", 0), "repeated_calls": values.get("repeated_calls", 0),
                         "nudges": values.get("nudges_used", 0), "corrections": values.get("corrections_used", 0),
                         "case_api_retries": sum(e.get("retries", 0) for e in evidence_log)
                         + sum(s.get("retries", 0) for s in steps if s["kind"] == "record"),
                         "model_calls": sum(1 for s in steps if s["kind"] in {"model", "finalize"})},
            "injected_faults": [s["injected_fault"] for s in steps if s.get("injected_fault")],
            "created_at": case["created_at"], "updated_at": case["updated_at"],
        }
        if include_steps:
            result["steps"] = steps
        return result

    def list(self, actor: Actor, status: str | None = None) -> list[dict[str, Any]]:
        return [{key: case[key] for key in ("case_id", "status", "outcome_reason", "store_id", "submitted_by",
                                            "proposal_version", "scenario_id", "updated_at")}
                for case in self.store.list(actor.store_ids, status)]

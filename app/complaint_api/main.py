"""Taco Alley complaint API (module 3): runs the LangGraph workflow with durable approval checkpoints.

Run: uvicorn complaint_api.main:app --host 0.0.0.0 --port 8090 --workers 1
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, quote

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from pydantic import BaseModel, Field

from taco_shared.agent.faults import FaultPlan, ModelFault
from taco_shared.agent.model import build_chat_model
from taco_shared.case_api_client import CaseApiClient
from taco_shared.config import InferenceSettings
from taco_shared.context import Actor
from taco_shared.db import AppDatabase
from taco_shared.paths import AGENT_SCENARIOS_V2, DATA_DIR, load_json
from taco_shared.policy import load_policy
from taco_shared.quality.store import DISPOSITIONS
from taco_shared.workflow import WorkflowDeps, WorkflowSettings

from . import ui
from .observability import (HTTP_REQUESTS, HTTP_SECONDS, REQUEST_ID, QualityCollector, ServedModel,
                            current_request_id, log, new_request_id, observe, versions_provider)
from .security import actor_from_id, current_actor
from .service import ComplaintService, ServiceSettings

STATE: dict[str, Any] = {}


def build_service() -> ComplaintService:
    inference = InferenceSettings.from_env()
    workflow = WorkflowSettings.from_env()
    timeout = float(os.environ.get("CASE_API_TIMEOUT_SECONDS") or 5)
    cases = CaseApiClient(inference.case_api_url, timeout=httpx.Timeout(timeout, connect=min(timeout, 3.0)),
                          retries=int(os.environ.get("CASE_API_RETRIES") or 2),
                          backoff_seconds=float(os.environ.get("CASE_API_BACKOFF_SECONDS") or 0.5))
    deps = WorkflowDeps(model=build_chat_model(inference, max_retries=int(os.environ.get("INFERENCE_MAX_RETRIES") or 2),
                                               request_id=current_request_id),
                        db=AppDatabase(Path(inference.app_db)), cases=cases, policy=load_policy(inference.policy_version),
                        settings=workflow, faults=FaultPlan(), observer=observe)
    STATE["inference"] = inference
    provider = versions_provider(ServedModel(inference.base_url, inference.model, inference.api_key),
                                 inference.policy_version)
    STATE["versions_provider"] = provider
    versions = provider()
    STATE["versions"] = versions
    quality_db = Path(os.environ.get("TACO_QUALITY_DB") or "/data/quality/quality.db")
    sample_rate = float(os.environ.get("QUALITY_SAMPLE_RATE") or 0)
    if sample_rate > 0 and not STATE.get("quality_collector"):
        STATE["quality_collector"] = QualityCollector(quality_db)
        REGISTRY.register(STATE["quality_collector"])
    return ComplaintService(deps, cases, ServiceSettings(
        state_dir=Path(os.environ.get("TACO_STATE_DIR") or "/data/state"),
        run_timeout_seconds=workflow.run_timeout_seconds,
        max_concurrent_runs=int(os.environ.get("WORKFLOW_MAX_CONCURRENT_RUNS") or 1),
        queue_timeout_seconds=float(os.environ.get("WORKFLOW_QUEUE_TIMEOUT_SECONDS") or 60),
        versions=versions, versions_provider=provider, quality_db=quality_db, quality_sample_rate=sample_rate))


@asynccontextmanager
async def lifespan(app: FastAPI):
    service = STATE.get("service") or build_service()
    STATE["service"] = service
    if os.environ.get("TACO_RESUME_ON_STARTUP", "1") != "0":
        threading.Thread(target=service.resume_incomplete, name="resume-incomplete", daemon=True).start()
    log("complaint_api_ready", **STATE.get("versions", {}), max_concurrent_runs=service.settings.max_concurrent_runs,
        tracing=os.environ.get("LANGSMITH_TRACING", "false"), trace_project=os.environ.get("LANGSMITH_PROJECT"),
        quality_sample_rate=service.settings.quality_sample_rate)
    yield


app = FastAPI(title="Taco Alley complaint API", lifespan=lifespan)


def service() -> ComplaintService:
    return STATE["service"]


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = new_request_id(request.headers.get("X-Request-ID"))
    token = REQUEST_ID.set(request_id)
    request.state.request_id = request_id
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        seconds = time.perf_counter() - started
        HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
        HTTP_SECONDS.labels(path).observe(seconds)
        if path not in {"/metrics", "/healthz"}:
            log("http_request", method=request.method, route=path, status=status, ms=round(seconds * 1000, 1),
                actor=request.headers.get("X-Actor-Id"))
        REQUEST_ID.reset(token)


# Health, graph, metrics -----------------------------------------------------------------------

@app.get("/healthz")
def healthz(remote: bool = True) -> dict[str, Any]:
    """Liveness is always ok when the process serves requests; remote dependencies are reported separately."""
    svc = service()
    inference: InferenceSettings | None = STATE.get("inference")
    if not remote:
        return {"status": "ok"}
    remote_status: dict[str, Any] = {"case_api": svc.cases.health()}
    if inference is not None:
        try:
            response = httpx.get(f"{inference.base_url}/models", timeout=3.0,
                                 headers={"Authorization": f"Bearer {inference.api_key}"})
            remote_status["inference"] = {"reachable": True, "status_code": response.status_code,
                                          "model_served": inference.model in response.text}
        except httpx.HTTPError as error:
            remote_status["inference"] = {"reachable": False, "error": type(error).__name__}
    return {"status": "ok", "model": svc.versions().get("model") if inference else None,
            "alias": inference.model if inference else None, "dependencies": remote_status}


@app.get("/graph", response_class=PlainTextResponse)
def graph() -> str:
    return service().graph.get_graph().draw_mermaid()


@app.get("/metrics")
def metrics() -> Response:
    service().versions()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# Complaints and cases ----------------------------------------------------------------------------

class IntakeRequest(BaseModel):
    scenario_id: str | None = Field(default=None, description="Demo shortcut: load the intake from agent-v2.")
    case_id: str | None = None
    customer_id: str | None = None
    complaint_date: str | None = None
    complaint_channel: str = "Website"
    complaint: str | None = Field(default=None, max_length=2000)


def resolve_intake(body: IntakeRequest) -> tuple[dict[str, Any], str | None]:
    if body.scenario_id:
        for scenario in load_json(Path(os.environ.get("TACO_SCENARIOS") or AGENT_SCENARIOS_V2))["scenarios"]:
            if scenario["scenario_id"] == body.scenario_id:
                return dict(scenario["intake"]), scenario["scenario_id"]
        raise HTTPException(status_code=404, detail="unknown scenario")
    intake = {"case_id": body.case_id, "customer_id": body.customer_id, "complaint_date": body.complaint_date,
              "complaint_channel": body.complaint_channel, "complaint": body.complaint}
    missing = [key for key, value in intake.items() if not value]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing fields: {missing}")
    return intake, None


@app.post("/complaints")
def submit(body: IntakeRequest, request: Request, actor: Actor = Depends(current_actor)) -> JSONResponse:
    intake, scenario_id = resolve_intake(body)
    view, created = service().submit(actor, intake, request.state.request_id, scenario_id)
    return JSONResponse({**view, "replayed": not created}, status_code=201 if created else 200)


@app.get("/cases")
def list_cases(status: str | None = Query(default=None), actor: Actor = Depends(current_actor)) -> list[dict[str, Any]]:
    return service().list(actor, status)


@app.get("/cases/{case_id}")
def get_case(case_id: str, steps: bool = False, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    service()._visible(actor, case_id)
    return service().view(case_id, include_steps=steps)


class ClarifyRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=500)


@app.post("/cases/{case_id}/clarify")
def clarify(case_id: str, body: ClarifyRequest, request: Request, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return service().clarify(actor, case_id, body.answer, request.state.request_id)


@app.post("/cases/{case_id}/retry")
def retry(case_id: str, request: Request, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return service().retry(actor, case_id, request.state.request_id)


@app.get("/reviews")
def reviews(actor: Actor = Depends(current_actor)) -> list[dict[str, Any]]:
    if actor.role not in {"reviewer", "area_manager"}:
        raise HTTPException(status_code=403, detail="only reviewers list pending reviews")
    return service().list(actor, "pending_review")


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject", "revise"]
    proposal_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=500)


@app.post("/reviews/{case_id}/decision")
def decide(case_id: str, body: DecisionRequest, request: Request, actor: Actor = Depends(current_actor)) -> JSONResponse:
    view, applied = service().decide(actor, case_id, body.decision, body.proposal_version, body.note,
                                     request.state.request_id)
    return JSONResponse({**view, "replayed": not applied})


# Admin (fault injection and fixture reset); disabled unless TACO_ADMIN_TOKEN is set -------------

def require_admin(request: Request) -> None:
    token = os.environ.get("TACO_ADMIN_TOKEN", "")
    if not token:
        raise HTTPException(status_code=404, detail="Not Found")
    supplied = request.headers.get("X-Admin-Token") or ""
    if not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=403, detail="admin token required")


class FaultsRequest(BaseModel):
    faults: list[ModelFault]


@app.get("/admin/faults", dependencies=[Depends(require_admin)])
def get_faults() -> dict[str, Any]:
    return {"faults": service().deps.faults.describe()}


@app.put("/admin/faults", dependencies=[Depends(require_admin)])
def put_faults(body: FaultsRequest) -> dict[str, Any]:
    service().deps.faults.set(body.faults)
    log("model_faults_set", faults=[f.model_dump() for f in body.faults])
    return {"faults": service().deps.faults.describe()}


@app.delete("/admin/faults", dependencies=[Depends(require_admin)])
def delete_faults() -> dict[str, Any]:
    service().deps.faults.clear()
    return {"faults": []}


@app.post("/admin/reset", dependencies=[Depends(require_admin)])
def reset(case_ids: list[str] | None = Body(default=None, embed=True)) -> dict[str, Any]:
    return {"reset": service().reset(case_ids)}


# Review page ------------------------------------------------------------------------------------------

def ui_actor(actor_id: str | None) -> Actor | None:
    try:
        return actor_from_id(actor_id)
    except HTTPException:
        return None


@app.get("/ui", response_class=HTMLResponse)
def ui_home(actor: str | None = None, status: str | None = None, message: str | None = None) -> str:
    who = ui_actor(actor)
    actors = [a for a in load_json(DATA_DIR / "actors.json")["actors"] if not a.get("synthetic")]
    reviewer = who is not None and who.role in {"reviewer", "area_manager"}
    status = status if status in ui.STATUSES else ("pending_review" if reviewer or who is None else "all")
    if who is None:
        return ui.page("Cases", None, ui.actor_picker(actors, status), message)
    cases = service().list(who)
    counts = {"all": len(cases)}
    for case in cases:
        counts[case["status"]] = counts.get(case["status"], 0) + 1
    shown = cases if status == "all" else [c for c in cases if c["status"] == status]
    body = ui.actor_picker(actors, status) + ui.status_tabs(who, status, counts) + ui.case_list(who, shown, status)
    if who.role in ui.QUALITY_ROLES and service().quality is not None:
        body = f"<p><a href='/ui/quality?actor={quote(who.actor_id)}'>Quality review queue</a></p>" + body
    return ui.page("Cases", who, body, message)


@app.get("/ui/cases/{case_id}", response_class=HTMLResponse)
def ui_case(case_id: str, actor: str | None = None, back: str = "pending_review", message: str | None = None) -> str:
    who = ui_actor(actor)
    if who is None:
        return ui.page("Case", None, "<p>Choose an actor on the <a href='/ui'>cases page</a>.</p>")
    service()._visible(who, case_id)
    back = back if back in ui.STATUSES else "all"
    return ui.page(f"Case {case_id}", who, ui.case_detail(who, service().view(case_id, include_steps=True), back),
                   message)


@app.post("/ui/cases/{case_id}/decision")
async def ui_decide(case_id: str, request: Request) -> RedirectResponse:
    form = {key: values[0] for key, values in parse_qs((await request.body()).decode("utf-8")).items()}
    who = actor_from_id(form.get("actor"))
    message = None
    try:
        await run_in_threadpool(service().decide, who, case_id, form.get("decision", ""),
                                int(form.get("proposal_version", "0")), form.get("note") or None,
                                request.state.request_id)
    except HTTPException as error:
        message = str(error.detail)
    target = f"/ui/cases/{quote(case_id)}?actor={quote(who.actor_id)}"
    return RedirectResponse(target + (f"&message={quote(message)}" if message else ""), status_code=303)


# Quality review pages (module 5) ----------------------------------------------------------------------

def quality_store():
    store = service().quality
    if store is None:
        raise HTTPException(status_code=404, detail="quality sampling is off (QUALITY_SAMPLE_RATE=0)")
    return store


def quality_reader(actor_id: str | None) -> Actor:
    who = ui_actor(actor_id)
    if who is None or who.role not in ui.QUALITY_ROLES:
        raise HTTPException(status_code=403, detail="only quality reviewers and area managers see quality samples")
    return who


@app.get("/ui/quality", response_class=HTMLResponse)
def ui_quality(actor: str | None = None, message: str | None = None) -> str:
    who = quality_reader(actor)
    store = quality_store()
    return ui.page("Quality review", who, ui.quality_list(who, store.queue(), store.counts()), message)


@app.get("/ui/quality/{sample_id}", response_class=HTMLResponse)
def ui_quality_sample(sample_id: str, actor: str | None = None, message: str | None = None) -> str:
    who = quality_reader(actor)
    store = quality_store()
    sample = store.sample(sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="sample not found")
    return ui.page(f"Quality sample {sample['case_id']}", who,
                   ui.quality_detail(who, sample, store.score(sample["sample_id"]), store.reviews(sample["sample_id"]),
                                     DISPOSITIONS), message)


@app.post("/ui/quality/{sample_id}/review")
async def ui_quality_review(sample_id: str, request: Request) -> RedirectResponse:
    form = {key: values[0] for key, values in parse_qs((await request.body()).decode("utf-8")).items()}
    who = quality_reader(form.get("actor"))
    message = None
    try:
        scores = {name: int(form[name]) for name, _ in ui.RUBRIC}
        quality_store().add_review(sample_id, who.actor_id, scores["tone"], scores["helpfulness"], scores["escalation"],
                                   form.get("safe_to_send") == "yes", form.get("disposition", ""),
                                   form.get("note") or None)
        log("quality_review", sample_id=sample_id, reviewer=who.actor_id, disposition=form.get("disposition"))
    except (KeyError, ValueError) as error:
        message = f"review not saved: {error}"
    target = f"/ui/quality/{quote(sample_id)}?actor={quote(who.actor_id)}"
    return RedirectResponse(target + (f"&message={quote(message)}" if message else ""), status_code=303)

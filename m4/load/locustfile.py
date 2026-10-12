"""Locust workloads for module 4 (run through load/run.py, which sets the environment below).

TACO_MODE=direct  chat completions straight to the gateway: screening prompts, facts inlined, json_schema
TACO_MODE=app     complaints through the complaint API as each case's rep; a synthetic reviewer approves
                  accepted proposals after a think time, without blocking the submitting user
TACO_SHAPE=steady TACO_USERS for TACO_DURATION seconds
TACO_SHAPE=burst  TACO_USERS for the first quarter, TACO_BURST_USERS for the middle half, TACO_USERS again
"""

from __future__ import annotations

import collections
import itertools
import json
import os
import random
import uuid

import gevent
from locust import HttpUser, LoadTestShape, between, events, task
from locust.exception import StopUser

from taco_shared.config import InferenceSettings
from taco_shared.load import LOAD_REVIEWER, direct_requests, load_cases

MODE = os.environ.get("TACO_MODE", "direct")
SEED = int(os.environ.get("TACO_SEED", "42"))
USERS = int(os.environ.get("TACO_USERS", "2"))
BURST_USERS = int(os.environ.get("TACO_BURST_USERS", "8"))
DURATION = int(os.environ.get("TACO_DURATION", "180"))
SHAPE = os.environ.get("TACO_SHAPE", "steady")
APPROVE = os.environ.get("TACO_APPROVE", "1") == "1"
THINK = (float(os.environ.get("TACO_THINK_MIN", "5")), float(os.environ.get("TACO_THINK_MAX", "15")))
REQUEST_TIMEOUT = float(os.environ.get("TACO_REQUEST_TIMEOUT", "300"))

random.seed(SEED)
OUTCOMES: collections.Counter[str] = collections.Counter()


class DirectUser(HttpUser):
    """Direct inference: isolates the model server from the application."""

    wait_time = between(0.5, 1.5)
    settings = InferenceSettings.from_env() if MODE == "direct" else None
    bodies = itertools.cycle(direct_requests(settings.model, settings.extra_body) if settings else [{}])

    @task
    def chat_completion(self) -> None:
        request = next(self.bodies)
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "X-Request-ID": f"load-{uuid.uuid4().hex[:16]}"}
        with self.client.post("/chat/completions", json=request["body"], headers=headers, name="chat_completions",
                              timeout=REQUEST_TIMEOUT, catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                OUTCOMES[f"http_{response.status_code}"] += 1
                return
            OUTCOMES[response.json()["choices"][0].get("finish_reason") or "unknown"] += 1


class AppUser(HttpUser):
    """End to end: the complaint API runs the LangGraph workflow, which calls the model and tools."""

    wait_time = between(1, 3)
    cases = collections.deque(load_cases() if MODE == "app" else [])

    @task
    def submit_complaint(self) -> None:
        if not self.cases:
            raise StopUser()
        case = self.cases.popleft()
        intake = {key: case[key] for key in ("case_id", "customer_id", "complaint_date", "complaint_channel", "complaint")}
        headers = {"X-Actor-Id": case["actor_id"], "X-Request-ID": f"load-{uuid.uuid4().hex[:16]}"}
        with self.client.post("/complaints", json=intake, headers=headers, name="/complaints", timeout=REQUEST_TIMEOUT,
                              catch_response=True) as response:
            if response.status_code not in (200, 201):
                response.failure(f"HTTP {response.status_code}")
                OUTCOMES[f"http_{response.status_code}"] += 1
                return
            view = response.json()
        OUTCOMES[view["status"]] += 1
        if APPROVE and view["status"] == "pending_review":
            gevent.spawn_later(random.uniform(*THINK), self.approve, case["case_id"], view["proposal_version"])

    def approve(self, case_id: str, version: int) -> None:
        headers = {"X-Actor-Id": LOAD_REVIEWER, "X-Request-ID": f"load-{uuid.uuid4().hex[:16]}"}
        with self.client.post(f"/reviews/{case_id}/decision", json={"decision": "approve", "proposal_version": version},
                              headers=headers, name="/reviews/[case]/decision", timeout=REQUEST_TIMEOUT,
                              catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                OUTCOMES[f"approve_http_{response.status_code}"] += 1
                return
            OUTCOMES[f"after_approval:{response.json()['status']}"] += 1


class TacoShape(LoadTestShape):
    def tick(self):
        elapsed = self.get_run_time()
        if elapsed >= DURATION:
            return None
        users = USERS
        if SHAPE == "burst" and DURATION * 0.25 <= elapsed < DURATION * 0.75:
            users = BURST_USERS
        return users, max(users, 1), [DirectUser if MODE == "direct" else AppUser]


@events.test_stop.add_listener
def write_outcomes(environment, **_) -> None:
    path = os.environ.get("TACO_OUTCOMES_FILE")
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(dict(OUTCOMES), handle, indent=2, sort_keys=True)

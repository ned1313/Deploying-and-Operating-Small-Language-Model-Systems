"""Trusted request context for the agent: who is asking and which data they may see."""

from __future__ import annotations

from dataclasses import dataclass

from .paths import DATA_DIR, load_json


@dataclass(frozen=True)
class AgentContext:
    actor_id: str
    store_ids: tuple[str, ...]
    customer_id: str
    case_id: str
    complaint_date: str


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str
    store_ids: tuple[str, ...]

    @property
    def can_submit(self) -> bool:
        return self.role == "service_rep"

    def can_review(self, store_id: str) -> bool:
        return self.role in {"reviewer", "area_manager"} and store_id in self.store_ids


def load_actor(actor_id: str) -> Actor:
    for actor in load_json(DATA_DIR / "actors.json")["actors"]:
        if actor["actor_id"] == actor_id:
            return Actor(actor["actor_id"], actor["role"], tuple(actor["store_ids"]))
    raise KeyError(f"unknown actor {actor_id!r}")


def actor_store_ids(actor_id: str) -> tuple[str, ...]:
    return load_actor(actor_id).store_ids


def build_context(actor_id: str, customer_id: str, case_id: str, complaint_date: str) -> AgentContext:
    return AgentContext(actor_id=actor_id, store_ids=actor_store_ids(actor_id), customer_id=customer_id,
                        case_id=case_id, complaint_date=complaint_date)

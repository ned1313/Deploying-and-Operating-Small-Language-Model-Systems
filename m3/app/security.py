"""Caller identity for the complaint API.

`X-Actor-Id` is an unauthenticated identity claim: the API trusts it because it listens on loopback
and serves synthetic data. Role and store scope always come from actors.json, never from the caller.
Inference authentication (the service key at the gateway) is separate and covers app -> model only.
"""

from __future__ import annotations

from fastapi import Header, HTTPException

from taco_shared.context import Actor, load_actor


def actor_from_id(actor_id: str | None) -> Actor:
    if not actor_id:
        raise HTTPException(status_code=401, detail="X-Actor-Id header is required")
    try:
        return load_actor(actor_id)
    except KeyError:
        raise HTTPException(status_code=401, detail="unknown actor") from None


def current_actor(x_actor_id: str | None = Header(default=None)) -> Actor:
    return actor_from_id(x_actor_id)

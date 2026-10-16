"""Caller identity for the complaint API.

APP_AUTH_MODE=header (modules 2-5, the default): `X-Actor-Id` is an unauthenticated identity claim. The
API trusts it because it listens on loopback and serves synthetic data; admin routes need TACO_ADMIN_TOKEN.

APP_AUTH_MODE=authelia (module 6): app-gateway (NGINX) asks Authelia to authenticate every request and
forwards `Remote-User` and `Remote-Groups`, plus a shared secret that proves the request came through the
gateway. Requests without the secret are rejected, so a container on the same network cannot forge the
headers. The role comes from the Authelia group; store scope still comes from actors.json.

Inference authentication (the client certificate at the model gateway) is separate and covers app -> model.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path

from fastapi import Header, HTTPException, Request

from taco_shared.context import Actor, load_actor

ROLES = ("service_rep", "reviewer", "area_manager", "quality_reviewer", "auditor", "admin")
GATEWAY_HEADER = "X-Taco-Gateway"


def auth_mode() -> str:
    return "authelia" if (os.environ.get("APP_AUTH_MODE") or "header").lower() == "authelia" else "header"


def _gateway_secret() -> str:
    path = os.environ.get("APP_GATEWAY_SECRET_FILE") or ""
    try:
        return Path(path).read_text(encoding="utf-8").strip() if path else ""
    except OSError:
        return ""


def through_gateway(request: Request) -> None:
    secret = _gateway_secret()
    supplied = request.headers.get(GATEWAY_HEADER) or ""
    if not secret or not hmac.compare_digest(supplied, secret):
        raise HTTPException(status_code=401, detail="requests must come through app-gateway")


def groups_of(request: Request) -> list[str]:
    return [g.strip() for g in (request.headers.get("Remote-Groups") or "").split(",") if g.strip()]


def actor_from_id(actor_id: str | None) -> Actor:
    if not actor_id:
        raise HTTPException(status_code=401, detail="X-Actor-Id header is required")
    try:
        return load_actor(actor_id)
    except KeyError:
        raise HTTPException(status_code=401, detail="unknown actor") from None


def actor_from_gateway(request: Request) -> Actor:
    through_gateway(request)
    user = request.headers.get("Remote-User")
    if not user:
        raise HTTPException(status_code=401, detail="not signed in")
    role = next((group for group in groups_of(request) if group in ROLES), None)
    if role is None:
        raise HTTPException(status_code=403, detail="no application role in the user's groups")
    try:
        store_ids = load_actor(user).store_ids
    except KeyError:
        raise HTTPException(status_code=403, detail="no store scope for this user") from None
    return Actor(user, role, store_ids)


def claimed_actor(request: Request) -> str | None:
    """Who the caller says they are, for logs and denial records; not proof of identity."""
    return request.headers.get("Remote-User") if auth_mode() == "authelia" else request.headers.get("X-Actor-Id")


def resolve_actor(request: Request, actor_id: str | None = None) -> Actor:
    actor = actor_from_gateway(request) if auth_mode() == "authelia" else actor_from_id(actor_id)
    request.state.actor = actor
    return actor


def current_actor(request: Request, x_actor_id: str | None = Header(default=None)) -> Actor:
    return resolve_actor(request, x_actor_id)


def require_admin(request: Request) -> None:
    if auth_mode() == "authelia":
        through_gateway(request)
        if "admin" not in groups_of(request):
            raise HTTPException(status_code=403, detail="admin group required")
        return
    token = os.environ.get("TACO_ADMIN_TOKEN", "")
    if not token:
        raise HTTPException(status_code=404, detail="Not Found")
    supplied = request.headers.get("X-Admin-Token") or ""
    if not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=403, detail="admin token required")

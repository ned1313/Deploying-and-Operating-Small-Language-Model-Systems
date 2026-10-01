"""Credential-free descriptions of API and transport errors."""

from __future__ import annotations

import re
from typing import Any


def redact(text: str) -> str:
    return re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", text)


def describe_error(error: Exception) -> dict[str, Any]:
    """Compact, credential-free description of an API or transport error."""
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    message = None
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict):
            message = inner.get("message") or inner.get("detail")
        else:
            message = str(inner)
    if not message:
        message = str(error)
    message = redact(" ".join(str(message).split()))[:1500]
    kind = "compatibility" if status in {400, 404, 405, 422} else "auth" if status in {401, 403} else \
        "rate_limit" if status == 429 else "server" if status and status >= 500 else "transport"
    return {"type": type(error).__name__, "status_code": status, "kind": kind, "message": message}

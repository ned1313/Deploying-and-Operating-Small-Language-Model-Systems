"""Client TLS for the module 6 gateway: verify the course root CA and present a client certificate.

Off unless INFERENCE_CA_FILE or INFERENCE_CLIENT_CERT_FILE is set, so modules 1-5 keep plain HTTP. The
SSL context reloads the client certificate when its file changes, so a rotated certificate is used by
new connections without a restart.
"""

from __future__ import annotations

import os
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class TlsFiles:
    ca_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None

    @classmethod
    def from_env(cls, prefix: str = "INFERENCE_") -> TlsFiles:
        return cls(os.environ.get(f"{prefix}CA_FILE") or None, os.environ.get(f"{prefix}CLIENT_CERT_FILE") or None,
                   os.environ.get(f"{prefix}CLIENT_KEY_FILE") or None)

    @property
    def enabled(self) -> bool:
        return bool(self.ca_file or self.cert_file)

    def describe(self) -> dict[str, Any]:
        return {"ca_file": self.ca_file, "client_cert_file": self.cert_file}

    def requests_args(self) -> dict[str, Any]:
        """verify/cert values for a requests session (Locust)."""
        args: dict[str, Any] = {"verify": self.ca_file or True}
        if self.cert_file:
            args["cert"] = (self.cert_file, self.key_file) if self.key_file else self.cert_file
        return args


class ReloadingContext:
    """An SSLContext whose client certificate follows the files on disk."""

    def __init__(self, files: TlsFiles) -> None:
        self.files = files
        self.context = ssl.create_default_context(cafile=files.ca_file)
        self._lock = threading.Lock()
        self._stamp: tuple[float, float] | None = None
        self.refresh()

    def _mtimes(self) -> tuple[float, float] | None:
        if not self.files.cert_file:
            return None
        key = self.files.key_file or self.files.cert_file
        return Path(self.files.cert_file).stat().st_mtime, Path(key).stat().st_mtime

    def refresh(self) -> None:
        stamp = self._mtimes()
        if stamp is None or stamp == self._stamp:
            return
        with self._lock:
            if stamp != self._stamp:
                self.context.load_cert_chain(self.files.cert_file, self.files.key_file)
                self._stamp = stamp


_CONTEXTS: dict[TlsFiles, ReloadingContext] = {}
_CONTEXTS_LOCK = threading.Lock()


def context_for(files: TlsFiles) -> ReloadingContext:
    with _CONTEXTS_LOCK:
        if files not in _CONTEXTS:
            _CONTEXTS[files] = ReloadingContext(files)
        return _CONTEXTS[files]


def verify_arg(files: TlsFiles | None = None) -> ssl.SSLContext | bool:
    """The `verify=` value for one-off httpx calls: the reloading context, or True when TLS is off."""
    files = files or TlsFiles.from_env()
    if not files.enabled:
        return True
    reloading = context_for(files)
    reloading.refresh()
    return reloading.context


def httpx_client(files: TlsFiles | None = None, **kwargs: Any) -> httpx.Client:
    files = files or TlsFiles.from_env()
    if not files.enabled:
        return httpx.Client(**kwargs)
    reloading = context_for(files)
    hooks = kwargs.pop("event_hooks", {})
    hooks.setdefault("request", []).insert(0, lambda request: reloading.refresh())
    return httpx.Client(verify=reloading.context, event_hooks=hooks, **kwargs)


def httpx_async_client(files: TlsFiles | None = None, **kwargs: Any) -> httpx.AsyncClient:
    files = files or TlsFiles.from_env()
    if not files.enabled:
        return httpx.AsyncClient(**kwargs)
    reloading = context_for(files)

    async def refresh(request: httpx.Request) -> None:
        reloading.refresh()

    return httpx.AsyncClient(verify=reloading.context, event_hooks={"request": [refresh]}, **kwargs)


def openai_http_client(asynchronous: bool = False) -> httpx.Client | httpx.AsyncClient | None:
    """`http_client=` for an OpenAI client: None (the SDK default) unless module 6 TLS is configured."""
    files = TlsFiles.from_env()
    if not files.enabled:
        return None
    return httpx_async_client(files) if asynchronous else httpx_client(files)

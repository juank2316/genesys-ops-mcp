from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import httpx
import pytest

from genesys_mcp import app
from genesys_mcp.client import AuditLog, GenesysClient
from genesys_mcp.config import OrgProfile, Settings


class FakeGenesys:
    """Records requests; `routes` maps (METHOD, path) -> handler(request) -> httpx.Response."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}
        self.token_requests = 0

    def on(self, method: str, path: str, handler=None, *, json_body=None, status: int = 200):
        if handler is None:
            handler = lambda req: httpx.Response(status, json=json_body)  # noqa: E731
        self.routes[(method, path)] = handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            self.token_requests += 1
            return httpx.Response(200, json={"access_token": f"tok{self.token_requests}", "expires_in": 86400})
        self.calls.append(request)
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return httpx.Response(404, json={"message": f"no route {request.method} {request.url.path}", "code": "not.found"})
        return handler(request)

    def bodies(self, method: str, path: str) -> list:
        return [json.loads(r.content) for r in self.calls if r.method == method and r.url.path == path and r.content]


def profile(name="lab", **kw) -> OrgProfile:
    kw.setdefault("environment", "lab" if name == "lab" else "prod")
    return OrgProfile(name=name, region="us-east-1", client_id="id", client_secret="secret", rate_per_sec=1000, max_concurrency=10, **kw)


def confirmed(tool, **kwargs):
    """Run a write tool through both steps (plan, then confirm) and return the executed result."""
    import asyncio

    plan = asyncio.run(tool(**kwargs))
    assert plan["status"] == "requiere_confirmacion", plan
    return asyncio.run(tool(**kwargs, confirmation_token=plan["confirmation_token"]))


@pytest.fixture
def fake(tmp_path: Path, monkeypatch):
    """Wire the app to a FakeGenesys with orgs 'lab' (writable) and 'cliente' (read_only)."""
    monkeypatch.setattr("genesys_mcp.client._backoff", lambda attempt: 0.0)
    server = FakeGenesys()
    transport = httpx.MockTransport(server.handle)
    profiles = {
        "lab": profile("lab", allow_delete=True),
        "cliente": profile("cliente", read_only=True, display_name="Cliente SA", aliases=("cli",)),
        "acme": profile("acme", allow_delete=True, display_name="ACME Colombia", aliases=("acme-co",)),
    }
    settings = Settings(orgs=profiles, source="test", data_dir=tmp_path)
    clients = {
        n: GenesysClient(p, audit=AuditLog(tmp_path / "audit.jsonl"), transport=transport) for n, p in profiles.items()
    }
    app.reset_for_tests(settings, clients)
    server.tmp_path = tmp_path
    yield server
    app.reset_for_tests(None)

import asyncio
import dataclasses
import json

import httpx
import pytest

from genesys_mcp import app
from genesys_mcp.client import GenesysAPIError, GenesysClient, GenesysError, PolicyError, is_write


def run(coro):
    return asyncio.run(coro)


def test_is_write():
    assert not is_write("GET", "/api/v2/users")
    assert not is_write("POST", "/api/v2/users/search")
    assert not is_write("POST", "/api/v2/analytics/conversations/details/query")
    assert is_write("POST", "/api/v2/users")
    assert is_write("DELETE", "/api/v2/routing/queues/x")


def test_token_is_cached(fake):
    fake.on("GET", "/api/v2/organizations/me", json_body={"id": "o"})
    c = app.client("lab")
    run(c.get("/api/v2/organizations/me"))
    run(c.get("/api/v2/organizations/me"))
    assert fake.token_requests == 1
    assert fake.calls[0].headers["Authorization"] == "Bearer tok1"


def test_read_retries_5xx(fake):
    attempts = []

    def flaky(req):
        attempts.append(1)
        return httpx.Response(503 if len(attempts) < 3 else 200, json={"ok": True})

    fake.on("GET", "/api/v2/users", flaky)
    assert run(app.client("lab").get("/api/v2/users")) == {"ok": True}
    assert len(attempts) == 3


def test_write_does_not_retry_5xx_but_retries_429(fake):
    fake.on("POST", "/api/v2/routing/queues", json_body={"message": "boom"}, status=503)
    with pytest.raises(GenesysAPIError):
        run(app.client("lab").request("POST", "/api/v2/routing/queues", json_body={"name": "q"}))
    assert len(fake.calls) == 1

    seq = []

    def throttled(req):
        seq.append(1)
        return httpx.Response(429, headers={"Retry-After": "0"}) if len(seq) == 1 else httpx.Response(200, json={"id": "q1"})

    fake.on("POST", "/api/v2/routing/skills", throttled)
    assert run(app.client("lab").request("POST", "/api/v2/routing/skills", json_body={"name": "s"})) == {"id": "q1"}
    assert len(seq) == 2


def test_401_refreshes_token_once(fake):
    seen = []

    def auth(req):
        seen.append(req.headers["Authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, json={"ok": 1})

    fake.on("GET", "/api/v2/users/me", auth)
    run(app.client("lab").get("/api/v2/users/me"))
    assert seen == ["Bearer tok1", "Bearer tok2"]


def test_read_only_and_delete_policies(fake):
    fake.on("POST", "/api/v2/users/search", json_body={"results": []})
    cliente = app.client("cliente")
    with pytest.raises(PolicyError, match="solo lectura"):
        run(cliente.request("POST", "/api/v2/users", json_body={}))
    run(cliente.request("POST", "/api/v2/users/search", json_body={}))  # search allowed
    assert [r.url.path for r in fake.calls] == ["/api/v2/users/search"]

    no_delete = GenesysClient(dataclasses.replace(app.settings().get("lab"), allow_delete=False))
    with pytest.raises(PolicyError, match="borrados"):
        run(no_delete.request("DELETE", "/api/v2/routing/queues/x"))


def test_api_error_message_is_actionable(fake):
    fake.on("GET", "/api/v2/routing/queues/x", json_body={"message": "Not allowed", "code": "missing.permissions"}, status=403)
    with pytest.raises(GenesysAPIError) as exc:
        run(app.client("lab").get("/api/v2/routing/queues/x"))
    assert "403" in str(exc.value) and "missing.permissions" in str(exc.value) and "permiso" in str(exc.value)


def test_paginate_page_count(fake):
    def pages(req):
        n = int(req.url.params["pageNumber"])
        return httpx.Response(200, json={"entities": [{"id": f"{n}-{i}"} for i in range(2)], "pageCount": 3})

    fake.on("GET", "/api/v2/routing/queues", pages)
    items = run(app.client("lab").paginate("/api/v2/routing/queues"))
    assert len(items) == 6
    assert len(run(app.client("lab").paginate("/api/v2/routing/queues", max_items=3))) == 3


def test_writes_are_audited_without_bodies(fake):
    fake.on("POST", "/api/v2/routing/skills", json_body={"id": "s1"})
    run(app.client("lab").request("POST", "/api/v2/routing/skills", json_body={"name": "secreto"}, tool="create_skills"))
    lines = (fake.tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    assert entry["org"] == "lab" and entry["tool"] == "create_skills" and entry["status"] == 200
    assert "secreto" not in lines[-1]


def test_transport_error_on_write_is_not_retried(fake):
    def boom(req):
        raise httpx.ConnectError("reset")

    fake.on("POST", "/api/v2/users", boom)
    with pytest.raises(GenesysError, match="incierto"):
        run(app.client("lab").request("POST", "/api/v2/users", json_body={}))
    assert len(fake.calls) == 1

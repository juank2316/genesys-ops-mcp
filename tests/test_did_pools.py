import asyncio
import json

import httpx
import pytest
from conftest import confirmed

from genesys_mcp.tools import telephony

POOLS = "/api/v2/telephony/providers/edges/didpools"
EXISTING = [
    {"id": "p1", "startPhoneNumber": "+576022979215", "endPhoneNumber": "+576022979215", "provider": "PURE_CLOUD", "comments": "Pruebas"},
    {"id": "p2", "startPhoneNumber": "+576087751785", "endPhoneNumber": "+576087751786", "provider": "PURE_CLOUD"},
    {"id": "p3", "startPhoneNumber": "+574554", "endPhoneNumber": "+574555", "provider": "PURE_CLOUD"},
]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def pools(fake):
    fake.on("GET", POOLS, json_body={"entities": EXISTING, "pageCount": 1})
    counter = iter(range(1, 1000))
    fake.on("POST", POOLS, lambda r: httpx.Response(200, json={**json.loads(r.content), "id": f"new{next(counter)}"}))
    return fake


def posts(fake):
    return [json.loads(r.content) for r in fake.calls if r.method == "POST" and r.url.path == POOLS]


def test_create_pools_plan_then_execute(pools):
    numbers = ["+576054368361", "+576087471763", "+576014437002"]
    plan = run(telephony.create_did_pools(numbers=numbers, org="acme", comments="Carga masiva"))
    assert plan["status"] == "requiere_confirmacion" and posts(pools) == []
    assert len(plan["cambios_planeados"]) == 3 and plan["advertencias"][0].startswith("ORG PRODUCTIVA")

    out = run(telephony.create_did_pools(numbers=numbers, org="acme", comments="Carga masiva",
                                         confirmation_token=plan["confirmation_token"]))
    assert [c["pool_id"] for c in out["created"]] == ["new1", "new2", "new3"]
    assert posts(pools)[0] == {
        "name": "+576054368361", "startPhoneNumber": "+576054368361", "endPhoneNumber": "+576054368361",
        "provider": "PURE_CLOUD", "comments": "Carga masiva",
    }


def test_numbers_already_covered_are_skipped(pools):
    out = confirmed(telephony.create_did_pools, numbers=["+576022979215", "+576087751786", "+576054368361"], org="lab")
    assert [c["number"] for c in out["created"]] == ["+576054368361"]
    assert {e["number"]: e["pool_id"] for e in out["ya_existian"]} == {"+576022979215": "p1", "+576087751786": "p2"}
    assert len(posts(pools)) == 1


def test_everything_already_exists_is_sin_cambios(pools):
    plan = run(telephony.create_did_pools(numbers=["+576022979215"], org="lab"))
    assert plan["status"] == "sin_cambios" and "confirmation_token" not in plan


def test_duplicates_in_input_create_one_pool(pools):
    plan = run(telephony.create_did_pools(numbers=["+576054368361", "+576054368361"], org="lab"))
    assert len(plan["cambios_planeados"]) == 1 and any("repetido" in w for w in plan["advertencias"])


@pytest.mark.parametrize("bad", ["605 4368361", "6054368361", "+57-605-4368361", "+57605"])
def test_bad_format_blocks_the_plan(pools, bad):
    plan = run(telephony.create_did_pools(numbers=["+576054368361", bad], org="lab"))
    assert plan["status"] == "bloqueado" and "E.164" in plan["problemas"][0] and posts(pools) == []


def test_read_only_org_blocked_and_cap(pools):
    plan = run(telephony.create_did_pools(numbers=["+576054368361"], org="cliente"))
    assert plan["status"] == "bloqueado" and "solo lectura" in plan["problemas"][0]
    many = [f"+5760{n:08d}" for n in range(telephony.MAX_DIDS_PER_CALL + 1)]
    assert "máximo" in run(telephony.create_did_pools(numbers=many, org="lab"))["problemas"][0]


def test_list_did_pools(pools):
    out = run(telephony.list_did_pools(org="lab"))
    assert out["count"] == 3
    assert run(telephony.list_did_pools(org="lab", contains="pruebas"))["pools"][0]["id"] == "p1"

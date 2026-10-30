import asyncio
import json

import httpx
import pytest
from conftest import confirmed

from genesys_mcp.tools import routing

WRAPUPS = "/api/v2/routing/wrapupcodes"
DIVISIONS = "/api/v2/authorization/divisions/home"
SOURCE = [{"id": f"s{i}", "name": n} for i, n in enumerate(["Venta", "Campaña Bienvenida", "Fallas", "Prueba", "venta"])]
TARGET = [{"id": "t1", "name": "fallas"}]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def wrapups(fake):
    """Source and target share one transport: the first client to authenticate (tok1) is the source."""

    def listing(request):
        items = SOURCE if request.headers["Authorization"] == "Bearer tok1" else TARGET
        return httpx.Response(200, json={"entities": items, "pageCount": 1})

    fake.on("GET", WRAPUPS, listing)
    fake.on("GET", DIVISIONS, json_body={"id": "home-id", "name": "Home"})
    fake.on("GET", "/api/v2/authorization/divisions", json_body={"entities": [{"id": "home-id", "name": "Home", "homeDivision": True}], "pageCount": 1})
    counter = iter(range(1, 1000))
    fake.on("POST", WRAPUPS, lambda r: httpx.Response(200, json={**json.loads(r.content), "id": f"new{next(counter)}"}))
    return fake


def test_plan_is_a_summary_then_execute_creates_only_missing(wrapups):
    args = {"org": "lab", "target_org": "acme"}
    plan = run(routing.copy_wrapup_codes(**args))
    assert plan["status"] == "requiere_confirmacion" and wrapups.bodies("POST", WRAPUPS) == []
    assert len(plan["cambios_planeados"]) == 1
    assert plan["vista_previa"]["a_crear_ahora"] == 3 and plan["vista_previa"]["ya_existian_en_destino"] == 1

    out = run(routing.copy_wrapup_codes(**args, confirmation_token=plan["confirmation_token"]))
    assert out["creados"] == 3 and out["pendientes"] == 0 and out["errores"] == 0
    assert [b["name"] for b in wrapups.bodies("POST", WRAPUPS)] == ["Venta", "Campaña Bienvenida", "Prueba"]
    assert wrapups.bodies("POST", WRAPUPS)[0]["division"] == {"id": "home-id"}


def test_max_create_leaves_pending_and_next_run_continues(wrapups):
    out = confirmed(routing.copy_wrapup_codes, org="lab", target_org="acme", max_create=2)
    assert out["creados"] == 2 and out["pendientes"] == 1


def test_name_filter_is_sent_to_the_source(wrapups):
    run(routing.copy_wrapup_codes(org="lab", target_org="acme", name="Ven*"))
    assert any(r.url.params.get("name") == "Ven*" for r in wrapups.calls if r.url.path == WRAPUPS)


def test_same_org_and_read_only_target_are_blocked(wrapups):
    assert run(routing.copy_wrapup_codes(org="lab", target_org="lab"))["status"] == "bloqueado"
    plan = run(routing.copy_wrapup_codes(org="lab", target_org="cliente"))
    assert plan["status"] == "bloqueado" and "solo lectura" in plan["problemas"][0]


def test_nothing_missing_is_sin_cambios(wrapups):
    wrapups.on("GET", WRAPUPS, json_body={"entities": TARGET, "pageCount": 1})
    assert run(routing.copy_wrapup_codes(org="lab", target_org="acme"))["status"] == "sin_cambios"

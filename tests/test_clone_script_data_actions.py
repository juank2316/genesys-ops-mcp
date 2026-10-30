import asyncio
import json

import httpx
import pytest
from conftest import confirmed

from genesys_mcp.tools import dataactions, scripts

SID = "11111111-1111-1111-1111-111111111111"
A = "custom_-_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"  # exists in source, must be created
B = "custom_-_bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"  # does not exist in source (dangling)
C = "custom_-_cccccccc-cccc-cccc-cccc-cccccccccccc"  # exists in source and already in target
ACTIONS = "/api/v2/integrations/actions"
SCRIPT_JSON = {"name": "Evolutivo", "pages": [{"a": A}, {"b": B}, {"c": C}, {"again": A}]}

SRC_INTEGRATIONS = [
    {"id": "s1", "name": "Web Services Data Actions", "integrationType": {"id": "custom-rest-actions"}},
    {"id": "s2", "name": "Genesys Cloud Data Actions Script", "integrationType": {"id": "purecloud-data-actions"}},
    {"id": "s3", "name": "Function Data Actions", "integrationType": {"id": "function-data-actions"}},
]
DST_INTEGRATIONS = [
    {"id": "d1", "name": "Web Services Data Actions", "integrationType": {"id": "custom-rest-actions"}, "reportedState": {"code": "ACTIVE"}},
    {"id": "d2", "name": "Genesys Cloud Data Actions", "integrationType": {"id": "purecloud-data-actions"}, "reportedState": {"code": "ACTIVE"}},
]


def run(coro):
    return asyncio.run(coro)


def page(items):
    return {"entities": items, "pageCount": 1}


def action(action_id, name, integration_id, category, **cfg):
    return {
        "id": action_id,
        "name": name,
        "integrationId": integration_id,
        "category": category,
        "secure": False,
        "contract": {
            "input": {"inputSchema": {"type": "object", "properties": {"telefono": {"type": "string"}}}},
            "output": {"successSchema": {"type": "object", "properties": {}}, "errorSchema": {"type": "object"}},
        },
        "config": {
            "request": {
                "requestUrlTemplate": "https://api.example.com/sms",
                "requestTemplateUri": f"{ACTIONS}/{action_id}/templates/requesttemplate.vm",
                "requestType": "POST",
                "headers": {"Apikey": "literal-key", "Authorization": "${input.Authorization}"},
            },
            "response": {"translationMap": {}, "successTemplateUri": f"{ACTIONS}/{action_id}/templates/successtemplate.vm"},
            **cfg,
        },
    }


@pytest.fixture
def world(fake, monkeypatch):
    """Source and target share one transport: the first client to authenticate (tok1) is the source."""
    monkeypatch.setattr(scripts, "_UPLOAD_POLL_SECONDS", 0)
    state = {"uploaded": None, "created": iter(["new-A"]), "published": []}

    def by_org(src, dst):
        return lambda r: httpx.Response(200, json=src if r.headers["Authorization"] == "Bearer tok1" else dst)

    fake.on("GET", f"/api/v2/scripts/{SID}", json_body={"id": SID, "name": "Evolutivo"})
    fake.on("POST", f"/api/v2/scripts/{SID}/export", json_body={"url": "https://s3.example.com/x.json"})
    fake.on("GET", "/x.json", json_body=SCRIPT_JSON)
    fake.on("GET", "/api/v2/integrations", by_org(page(SRC_INTEGRATIONS), page(DST_INTEGRATIONS)))
    fake.on("GET", ACTIONS, json_body=page([{"id": "new-C", "name": "Consultar ID WrapUp Code", "integrationId": "d2"}]))
    fake.on("GET", f"{ACTIONS}/{A}", json_body=action(A, "EnvioSMS", "s1", "Web Services Data Actions"))
    fake.on("GET", f"{ACTIONS}/{B}", json_body={"message": "not found", "code": "not.found"}, status=404)
    fake.on("GET", f"{ACTIONS}/{C}", json_body=action(C, "Consultar ID WrapUp Code", "s2", "Genesys Cloud Data Actions Script"))
    for aid in (A, C):
        fake.on("GET", f"{ACTIONS}/{aid}/templates/requesttemplate.vm", lambda r: httpx.Response(200, text='{"to": "${input.telefono}"}'))
        fake.on("GET", f"{ACTIONS}/{aid}/templates/successtemplate.vm", lambda r: httpx.Response(200, text="${rawResult}"))
    fake.on("POST", ACTIONS, lambda r: httpx.Response(200, json={**json.loads(r.content), "id": next(state["created"])}))
    fake.on("GET", f"{ACTIONS}/new-A/draft", json_body={"id": "new-A", "version": 1})
    fake.on("POST", f"{ACTIONS}/new-A/draft/publish", lambda r: state["published"].append(json.loads(r.content)) or httpx.Response(200, json={}))

    def upload(req):
        state["uploaded"] = req.content.decode("utf-8", "replace")
        return httpx.Response(200, json={"correlationId": "c1"})

    fake.on("POST", "/uploads/v2/scripter", upload)
    fake.on("GET", "/api/v2/scripts/uploads/c1/status", json_body={"succeeded": True})
    fake.on("GET", "/api/v2/scripts", json_body=page([{"id": "new-script", "name": "Evolutivo BKP"}]))
    fake.on("POST", "/api/v2/scripts/published", json_body={"id": "new-script", "name": "Evolutivo BKP"})
    fake.state = state
    return fake


ARGS = dict(script=SID, new_name="Evolutivo BKP", org="lab", target_org="acme")


def test_plan_only_records_the_missing_action_and_warns(world):
    plan = run(dataactions.clone_script_with_data_actions(**ARGS))
    assert plan["status"] == "requiere_confirmacion"
    paths = [w["path"] for w in plan["cambios_planeados"]]
    assert paths == [ACTIONS, "https://apps.mypurecloud.com/uploads/v2/scripter", "/api/v2/scripts/published"]  # C reused, B skipped
    assert world.state["uploaded"] is None and world.bodies("POST", ACTIONS) == []
    warnings = " ".join(plan["advertencias"])
    assert "Genesys Cloud Data Actions Script" in warnings and "mismo tipo" in warnings  # integration fallback
    assert "Apikey" in warnings and "literal-key" not in json.dumps(plan)  # secret named, never shown
    assert B in warnings and "credenciales de las integraciones no se copian" in warnings


def test_execute_creates_publishes_and_rewrites_ids(world):
    out = confirmed(dataactions.clone_script_with_data_actions, **ARGS)
    assert [(d["nombre"], d["estado"], d["destino"]) for d in out["data_actions"]] == [
        ("EnvioSMS", "creado", "new-A"),
        ("Consultar ID WrapUp Code", "ya existía (se reutiliza)", "new-C"),
    ]
    assert out["referencias_sin_origen"] == [B]
    body = world.bodies("POST", ACTIONS)[0]
    assert body["integrationId"] == "d1" and body["category"] == "Web Services Data Actions"
    assert body["config"]["request"]["requestTemplate"] == '{"to": "${input.telefono}"}'
    assert body["config"]["response"]["successTemplate"] == "${rawResult}"
    assert body["config"]["request"]["headers"]["Apikey"] == "literal-key"
    assert body["contract"]["input"]["inputSchema"]["properties"] == {"telefono": {"type": "string"}}
    assert "errorSchema" not in json.dumps(body["contract"])
    assert world.state["published"] == [{"version": 1}]
    uploaded = world.state["uploaded"]
    assert "new-A" in uploaded and "new-C" in uploaded and B in uploaded
    assert A not in uploaded and C not in uploaded
    assert out["target"]["script"] == {"id": "new-script", "name": "Evolutivo BKP"}


def test_integration_map_overrides_the_fallback(world):
    plan = run(dataactions.clone_script_with_data_actions(**ARGS, integration_map={"Genesys Cloud Data Actions Script": "d2"}))
    assert not any("mismo tipo" in w for w in plan["advertencias"])
    bad = run(dataactions.clone_script_with_data_actions(**ARGS, integration_map={"Genesys Cloud Data Actions Script": "nope"}))
    assert bad["status"] == "bloqueado" and "integration_map" in bad["problemas"][0]


def test_function_actions_and_same_org_and_read_only_are_blocked(world):
    world.on("GET", f"{ACTIONS}/{C}", json_body=action(C, "Fn", "s3", "Function Data Actions"))
    plan = run(dataactions.clone_script_with_data_actions(**ARGS))
    assert plan["status"] == "bloqueado" and "Function" in plan["problemas"][0]
    same = run(dataactions.clone_script_with_data_actions(**{**ARGS, "target_org": "lab"}))
    assert same["status"] == "bloqueado" and "misma org" in same["problemas"][0]
    ro = run(dataactions.clone_script_with_data_actions(**{**ARGS, "target_org": "cliente"}))
    assert ro["status"] == "bloqueado" and "solo lectura" in ro["problemas"][0]
    assert world.bodies("POST", ACTIONS) == []

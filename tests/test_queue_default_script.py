import asyncio
import json

import httpx
import pytest
from conftest import confirmed

from genesys_mcp.tools import queues

QUEUES = "/api/v2/routing/queues"
SID = "11111111-1111-1111-1111-111111111111"
OLD = "22222222-2222-2222-2222-222222222222"


def run(coro):
    return asyncio.run(coro)


def feature(enabled):
    return {"properties": {"enabled": {"typeName": "boolean", "value": enabled}}}


SCRIPT = {
    "id": SID,
    "name": "Evolutivo",
    "features": {"scripter": feature(True), "callback": feature(True), "chat": feature(False), "email": feature(True), "message": feature(True)},
}


def queue(qid, name, scripts=None):
    return {
        "id": qid, "name": name, "division": {"id": "d1", "name": "Home"}, "memberCount": 3, "dateModified": "x",
        "mediaSettings": {"call": {"alertingTimeoutSeconds": 8}}, "skillEvaluationMethod": "ALL",
        "defaultScripts": {m: {"id": s, "selfUri": f"/api/v2/scripts/{s}"} for m, s in (scripts or {}).items()},
    }


ALL4 = {"CALL": SID, "CALLBACK": SID, "EMAIL": SID, "MESSAGE": SID}
QUEUE_DATA = {
    "q1": queue("q1", "Contigencia A"),
    "q2": queue("q2", "Contigencia B", {"CALL": OLD}),
    "q3": queue("q3", "Back RRSS", ALL4),
}


@pytest.fixture
def world(fake):
    fake.on("GET", f"/api/v2/scripts/{SID}", json_body=SCRIPT)
    fake.on("GET", "/api/v2/scripts", json_body={"entities": [{"id": SID, "name": "Evolutivo"}, {"id": OLD, "name": "Script viejo"}], "pageCount": 1})
    fake.on("GET", QUEUES, json_body={"entities": [{"id": q, "name": d["name"]} for q, d in QUEUE_DATA.items()], "pageCount": 1})
    for qid, data in QUEUE_DATA.items():
        fake.on("GET", f"{QUEUES}/{qid}", json_body=data)
        fake.on("PUT", f"{QUEUES}/{qid}", lambda r: httpx.Response(200, json=json.loads(r.content)))
    return fake


def puts(fake):
    return {r.url.path.rsplit("/", 1)[1]: json.loads(r.content) for r in fake.calls if r.method == "PUT"}


def test_plan_respects_existing_scripts_and_shows_before_after(world):
    plan = run(queues.set_queue_default_script(script=SID, org="lab", all_queues=True))
    assert plan["status"] == "requiere_confirmacion" and puts(world) == {}
    assert [json.loads(w["body"])["cola"] for w in plan["cambios_planeados"]] == ["Contigencia A", "Contigencia B"]  # q3 is already set
    view = plan["vista_previa"]
    assert view["canales"] == ["CALL", "CALLBACK", "EMAIL", "MESSAGE"]  # chat is disabled in the script
    assert view["colas_evaluadas"] == 3 and view["colas_sin_cambios"] == 1
    assert view["respetados_por_script_existente"] == [{"cola": "Contigencia B", "canal": "CALL", "script_actual": "Script viejo"}]
    assert any("NO se reemplazan" in w for w in plan["advertencias"])


def test_execute_sets_only_default_scripts_and_keeps_the_rest(world):
    confirmed(queues.set_queue_default_script, script=SID, org="lab", all_queues=True)
    sent = puts(world)
    assert set(sent) == {"q1", "q2"}
    assert sent["q1"]["defaultScripts"] == {m: {"id": SID} for m in ALL4}
    assert sent["q2"]["defaultScripts"]["CALL"]["id"] == OLD  # not replaced
    assert {m: v["id"] for m, v in sent["q2"]["defaultScripts"].items() if m != "CALL"} == {m: SID for m in ("CALLBACK", "EMAIL", "MESSAGE")}
    assert sent["q1"]["name"] == "Contigencia A" and sent["q1"]["mediaSettings"] == {"call": {"alertingTimeoutSeconds": 8}}
    assert "memberCount" not in sent["q1"] and "dateModified" not in sent["q1"]


def test_replace_existing_overwrites_and_reports_the_old_script(world):
    plan = run(queues.set_queue_default_script(script=SID, org="lab", all_queues=True, replace_existing=True))
    q2 = next(c for c in plan["vista_previa"]["colas_a_cambiar"] if c["cola"] == "Contigencia B")
    assert q2["cambios"]["CALL"] == {"antes": "Script viejo", "despues": "Evolutivo"}
    assert plan["vista_previa"]["respetados_por_script_existente"] == []
    confirmed(queues.set_queue_default_script, script=SID, org="lab", all_queues=True, replace_existing=True)
    assert puts(world)["q2"]["defaultScripts"]["CALL"]["id"] == SID


def test_selection_modes_and_media_override(world):
    for bad in ({}, {"all_queues": True, "name_pattern": "x*"}, {"queues": ["q1"], "all_queues": True}):
        assert "exactamente una" in run(queues.set_queue_default_script(script=SID, org="lab", **bad))["problemas"][0]
    plan = run(queues.set_queue_default_script(script=SID, org="lab", queues=["33333333-3333-3333-3333-333333333333"]))
    assert plan["status"] == "bloqueado"  # unknown queue id: the read fails instead of guessing
    only_chat = run(queues.set_queue_default_script(script=SID, org="lab", all_queues=True, media_types=["CHAT"]))
    assert any("no tiene habilitado el canal CHAT" in w for w in only_chat["advertencias"])


def test_read_only_org_is_blocked(world):
    plan = run(queues.set_queue_default_script(script=SID, org="cliente", all_queues=True))
    assert plan["status"] == "bloqueado" and "solo lectura" in plan["problemas"][0]
    assert puts(world) == {}

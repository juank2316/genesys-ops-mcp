import asyncio
import json
import os
import time

import httpx
import pytest
from conftest import confirmed

from genesys_mcp.config import ConfigError
from genesys_mcp.tools import api, orgs, queues, routing, scripts, users

HOME = {"id": "11111111-1111-1111-1111-111111111111", "name": "Home"}
USER_ID = "22222222-2222-2222-2222-222222222222"
QUEUE_ID = "33333333-3333-3333-3333-333333333333"


def run(coro):
    return asyncio.run(coro)


def listing(*entities):
    return {"entities": list(entities), "pageCount": 1}


def writes(fake):
    return [(r.method, r.url.path) for r in fake.calls if r.method != "GET" and not r.url.path.endswith("/search")]


# -- org is mandatory -------------------------------------------------------


def test_org_is_required_everywhere(fake):
    with pytest.raises(ConfigError, match="Falta indicar el cliente"):
        run(queues.list_queues(org=None))
    with pytest.raises(ConfigError, match="Falta indicar el cliente"):
        run(queues.delete_queue(queue=QUEUE_ID, org=""))
    with pytest.raises(ConfigError, match="no está configurada"):
        run(queues.list_queues(org="otra"))
    assert fake.calls == []


def test_org_resolves_by_alias(fake):
    fake.on("GET", "/api/v2/routing/queues", json_body=listing())
    assert run(queues.list_queues(org="ACME-CO"))["org"] == "acme"


def test_list_orgs_shows_tags_without_secrets(fake):
    out = run(orgs.list_orgs())
    acme = next(o for o in out["orgs"] if o["org"] == "acme")
    assert acme["display_name"] == "ACME Colombia" and acme["environment"] == "prod" and acme["aliases"] == ["acme-co"]
    assert "default_org" not in out and "secret" not in json.dumps(out)


# -- plan + confirmation ------------------------------------------------------


def test_plan_sends_no_writes_and_token_executes_once(fake):
    fake.on("GET", f"/api/v2/routing/queues/{QUEUE_ID}", json_body={"id": QUEUE_ID, "name": "Ventas", "memberCount": 3})
    fake.on("DELETE", f"/api/v2/routing/queues/{QUEUE_ID}", status=204, json_body=None)

    plan = run(queues.delete_queue(queue=QUEUE_ID, org="acme"))
    assert plan["status"] == "requiere_confirmacion"
    assert plan["org"] == ["ACME Colombia (acme) · PROD · us-east-1"]
    assert plan["cambios_planeados"] == [{"org": "acme", "method": "DELETE", "path": f"/api/v2/routing/queues/{QUEUE_ID}"}]
    assert plan["advertencias"][0].startswith("ORG PRODUCTIVA") and "irreversible" in plan["advertencias"][-1]
    assert writes(fake) == []

    token = plan["confirmation_token"]
    out = run(queues.delete_queue(queue=QUEUE_ID, org="acme-co", confirmation_token=token))  # alias == same plan
    assert out["deleted"]["name"] == "Ventas" and out["ejecutado_en"] == plan["org"]
    assert writes(fake) == [("DELETE", f"/api/v2/routing/queues/{QUEUE_ID}")]

    with pytest.raises(Exception, match="inválido, ya usado o vencido"):
        run(queues.delete_queue(queue=QUEUE_ID, org="acme", confirmation_token=token))
    assert len(writes(fake)) == 1


def test_token_is_bound_to_org_and_arguments(fake):
    fake.on("GET", f"/api/v2/routing/queues/{QUEUE_ID}", json_body={"id": QUEUE_ID, "name": "Ventas"})
    token = run(queues.delete_queue(queue=QUEUE_ID, org="acme"))["confirmation_token"]
    with pytest.raises(Exception, match="no coinciden"):
        run(queues.delete_queue(queue=QUEUE_ID, org="lab", confirmation_token=token))
    token = run(queues.delete_queue(queue=QUEUE_ID, org="acme"))["confirmation_token"]
    with pytest.raises(Exception, match="no coinciden"):
        run(queues.delete_queue(queue="44444444-4444-4444-4444-444444444444", org="acme", confirmation_token=token))
    assert writes(fake) == []


def test_token_expires(fake, monkeypatch):
    fake.on("GET", f"/api/v2/routing/queues/{QUEUE_ID}", json_body={"id": QUEUE_ID, "name": "Ventas"})
    token = run(queues.delete_queue(queue=QUEUE_ID, org="lab"))["confirmation_token"]
    later = time.monotonic() + 3600
    monkeypatch.setattr("genesys_mcp.confirm.time.monotonic", lambda: later)
    with pytest.raises(Exception, match="venció|inválido"):
        run(queues.delete_queue(queue=QUEUE_ID, org="lab", confirmation_token=token))
    assert writes(fake) == []


def test_missing_data_blocks_the_plan(fake):
    fake.on("GET", "/api/v2/routing/queues", json_body=listing())
    fake.on("GET", "/api/v2/authorization/divisions", json_body=listing())
    plan = run(queues.create_queue(name="Nueva", division="NoExiste", org="lab"))
    assert plan["status"] == "bloqueado" and "confirmation_token" not in plan
    assert "división" in plan["problemas"][0] and "NoExiste" in plan["problemas"][0]
    assert writes(fake) == []


def test_audit_records_the_confirmation(fake):
    fake.on("GET", "/api/v2/routing/skills", json_body=listing())
    fake.on("POST", "/api/v2/routing/skills", json_body={"id": "s1", "name": "X"})
    confirmed(routing.create_skills, names=["X"], org="lab")
    entry = json.loads((fake.tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["tool"] == "create_skills" and entry["confirmation"]


# -- tools -----------------------------------------------------------------


def test_create_skills_is_idempotent(fake):
    fake.on("GET", "/api/v2/routing/skills", json_body=listing({"id": "s1", "name": "Ventas"}))
    fake.on("POST", "/api/v2/routing/skills", lambda r: httpx.Response(200, json={"id": "s2", **json.loads(r.content)}))
    out = confirmed(routing.create_skills, names=["ventas", "Soporte", "Soporte"], org="lab")
    assert [x["name"] for x in out["created"]] == ["Soporte"]
    assert [x["name"] for x in out["already_existed"]] == ["Ventas"]
    assert fake.bodies("POST", "/api/v2/routing/skills") == [{"name": "Soporte"}]


def test_create_queue_new_with_members(fake):
    fake.on("GET", "/api/v2/routing/queues", json_body=listing())
    fake.on("GET", "/api/v2/authorization/divisions/home", json_body=HOME)
    fake.on("POST", "/api/v2/routing/queues", lambda r: httpx.Response(200, json={"id": "q1", **json.loads(r.content)}))
    fake.on("POST", "/api/v2/routing/queues/q1/members", status=204, json_body=None)
    fake.on("POST", "/api/v2/users/search", json_body={"results": [{"id": USER_ID}], "pageCount": 1})
    kwargs = dict(name="Soporte N2", settings={"acwSettings": {"timeoutMs": 30000}}, members=["ana@x.com"], org="lab")
    plan = run(queues.create_queue(**kwargs))
    assert [w["path"] for w in plan["cambios_planeados"]] == [
        "/api/v2/routing/queues", "/api/v2/routing/queues/<id-nuevo>/members"
    ]
    out = run(queues.create_queue(**kwargs, confirmation_token=plan["confirmation_token"]))
    assert out["status"] == "created"
    body = fake.bodies("POST", "/api/v2/routing/queues")[0]
    assert body["division"] == {"id": HOME["id"]} and body["acwSettings"]["timeoutMs"] == 30000
    assert fake.bodies("POST", "/api/v2/routing/queues/q1/members") == [[{"id": USER_ID}]]


def test_create_queue_existing_needs_no_confirmation(fake):
    fake.on("GET", "/api/v2/routing/queues", json_body=listing({"id": "q1", "name": "Soporte N2"}))
    out = run(queues.create_queue(name="soporte n2", org="lab"))
    assert out["status"] == "sin_cambios" and out["resultado"]["status"] == "already_existed"
    assert writes(fake) == []


def test_update_queue_deep_merges_and_drops_readonly(fake):
    current = {"id": QUEUE_ID, "name": "Q", "memberCount": 4, "acwSettings": {"wrapupPrompt": "MANDATORY", "timeoutMs": 1000}}
    fake.on("GET", f"/api/v2/routing/queues/{QUEUE_ID}", json_body=current)
    fake.on("PUT", f"/api/v2/routing/queues/{QUEUE_ID}", lambda r: httpx.Response(200, json=json.loads(r.content)))
    confirmed(queues.update_queue, queue=QUEUE_ID, changes={"acwSettings": {"timeoutMs": 60000}}, org="lab")
    body = fake.bodies("PUT", f"/api/v2/routing/queues/{QUEUE_ID}")[0]
    assert body["acwSettings"] == {"wrapupPrompt": "MANDATORY", "timeoutMs": 60000}
    assert "memberCount" not in body


def _onboarding_routes(fake):
    fake.on("POST", "/api/v2/users/search", json_body={"results": [], "pageCount": 1})
    fake.on("GET", "/api/v2/authorization/divisions/home", json_body=HOME)
    fake.on("POST", "/api/v2/users", json_body={"id": USER_ID})
    fake.on("GET", "/api/v2/authorization/roles", json_body=listing({"id": "r1", "name": "Agent"}))
    fake.on("POST", f"/api/v2/authorization/subjects/{USER_ID}/bulkadd", status=204, json_body=None)
    fake.on("GET", "/api/v2/routing/skills", json_body=listing({"id": "s1", "name": "Ventas"}))
    fake.on("PATCH", f"/api/v2/users/{USER_ID}/routingskills/bulk", json_body={"entities": []})
    fake.on("GET", f"/api/v2/users/{USER_ID}/routingskills", json_body=listing({"name": "Ventas", "proficiency": 4}))
    fake.on("GET", "/api/v2/routing/queues", json_body=listing())


def test_create_user_with_unknown_queue_is_blocked_before_creating_anything(fake):
    _onboarding_routes(fake)
    plan = run(
        users.create_user(
            name="Ana", email="ana@x.com", roles=["Agent"], queues=["NoExiste"], org="lab",
            skills=[users.ProficiencyItem(name="Ventas", proficiency=4)],
        )
    )
    assert plan["status"] == "bloqueado" and any("NoExiste" in p for p in plan["problemas"])
    assert writes(fake) == []


def test_create_user_onboarding_reports_each_step(fake):
    _onboarding_routes(fake)
    out = confirmed(
        users.create_user,
        name="Ana", email="ana@x.com", roles=["Agent"], org="lab",
        skills=[users.ProficiencyItem(name="Ventas", proficiency=4)],
    )
    assert out["steps"]["user"]["status"] == "created"
    assert out["steps"]["roles"]["status"] == "ok"
    assert out["steps"]["skills"]["result"]["current"] == [{"name": "Ventas", "proficiency": 4}]
    assert fake.bodies("POST", f"/api/v2/authorization/subjects/{USER_ID}/bulkadd") == [
        {"grants": [{"roleId": "r1", "divisionId": HOME["id"]}]}
    ]
    assert fake.bodies("PATCH", f"/api/v2/users/{USER_ID}/routingskills/bulk") == [[{"id": "s1", "proficiency": 4.0}]]


def test_update_user_sends_version(fake):
    fake.on("GET", f"/api/v2/users/{USER_ID}", json_body={"id": USER_ID, "version": 7})
    fake.on("PATCH", f"/api/v2/users/{USER_ID}", lambda r: httpx.Response(200, json={"id": USER_ID, **json.loads(r.content)}))
    confirmed(users.update_user, user=USER_ID, changes={"title": "Supervisor"}, org="lab")
    assert fake.bodies("PATCH", f"/api/v2/users/{USER_ID}") == [{"title": "Supervisor", "version": 7}]
    plan = run(users.update_user(user=USER_ID, changes={"password": "x"}, org="lab"))
    assert plan["status"] == "bloqueado" and "no soportados" in plan["problemas"][0]
    assert plan["parametros"]["changes"] == {"password": "***"}


def test_read_only_org_blocks_the_plan(fake):
    fake.on("GET", "/api/v2/routing/skills", json_body=listing())
    plan = run(routing.create_skills(names=["X"], org="cliente"))
    assert plan["status"] == "bloqueado" and "solo lectura" in plan["problemas"][0]
    fake.on("GET", "/api/v2/authorization/divisions/home", json_body=HOME)
    plan = run(users.set_user_roles(user=USER_ID, roles=["11111111-1111-1111-1111-111111111112"], org="cli"))
    assert plan["status"] == "bloqueado" and plan["org"] == ["Cliente SA (cliente) · PROD · us-east-1"]
    assert writes(fake) == []


def test_import_script_flow(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(scripts, "_UPLOAD_POLL_SECONDS", 0)
    src = tmp_path / "plantilla.json"
    src.write_text(json.dumps({"name": "{{NOMBRE}}", "pages": []}), encoding="utf-8")
    uploaded = {}

    def upload(req):
        uploaded["body"] = req.content.decode("utf-8", "replace")
        return httpx.Response(200, json={"correlationId": "c1"})

    statuses = iter([{"succeeded": False}, {"succeeded": True}])
    fake.on("POST", "/uploads/v2/scripter", upload)
    fake.on("GET", "/api/v2/scripts/uploads/c1/status", lambda r: httpx.Response(200, json=next(statuses)))
    fake.on("GET", "/api/v2/scripts", json_body=listing({"id": "sc1", "name": "Encuesta"}))
    fake.on("POST", "/api/v2/scripts/published", json_body={"id": "sc1", "name": "Encuesta"})
    kwargs = dict(file_path=str(src), script_name="Encuesta", substitutions={"NOMBRE": "Encuesta"}, org="lab")
    plan = run(scripts.import_script(**kwargs))
    assert [w["path"] for w in plan["cambios_planeados"]] == [
        "https://apps.mypurecloud.com/uploads/v2/scripter",  # the upload service lives on apps.<domain>, not api.
        "/api/v2/scripts/published",
    ]
    assert "body" not in uploaded
    out = run(scripts.import_script(**kwargs, confirmation_token=plan["confirmation_token"]))
    assert out["script"] == {"id": "sc1", "name": "Encuesta"} and out["published"]
    assert 'name="scriptName"' in uploaded["body"] and '"name": "Encuesta"' in uploaded["body"]
    assert fake.bodies("POST", "/api/v2/scripts/published") == [{"scriptId": "sc1"}]


def test_errors_reach_the_model_through_mcp(fake):
    from genesys_mcp import server  # noqa: F401  (registers tools)
    from genesys_mcp.app import mcp

    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    with pytest.raises(ToolError, match="pasá el id o el email") as exc:
        run(mcp.call_tool("get_user", {"user": "nadie", "org": "lab"}))
    assert not isinstance(exc.value, UnexpectedToolError)  # not the generic crash message
    with pytest.raises(ToolError, match="Falta indicar el cliente|required"):
        run(mcp.call_tool("delete_queue", {"queue": QUEUE_ID}))


def test_plan_through_mcp_with_pydantic_args(fake):
    from genesys_mcp import server  # noqa: F401
    from genesys_mcp.app import mcp

    fake.on("GET", "/api/v2/routing/skills", json_body=listing({"id": "s1", "name": "Ventas"}))
    fake.on("PATCH", f"/api/v2/users/{USER_ID}/routingskills/bulk", json_body={"entities": []})
    fake.on("GET", f"/api/v2/users/{USER_ID}/routingskills", json_body=listing())
    args = {"user": USER_ID, "skills": [{"name": "Ventas", "proficiency": 2}], "org": "lab"}
    plan = json.loads(run(mcp.call_tool("set_user_skills", args)).content[0].text)
    assert plan["status"] == "requiere_confirmacion"
    run(mcp.call_tool("set_user_skills", {**args, "confirmation_token": plan["confirmation_token"]}))
    assert fake.bodies("PATCH", f"/api/v2/users/{USER_ID}/routingskills/bulk") == [[{"id": "s1", "proficiency": 2.0}]]


def test_clone_script_across_orgs_respects_target_policy(fake):
    fake.on("GET", "/api/v2/scripts", json_body=listing({"id": "sc1", "name": "Encuesta"}))
    fake.on("GET", "/api/v2/scripts/sc1", json_body={"id": "sc1", "name": "Encuesta"})
    fake.on("POST", "/api/v2/scripts/sc1/export", json_body={"url": "https://s3.example.com/x.json"})
    fake.on("GET", "/x.json", json_body={"pages": []})
    plan = run(scripts.clone_script(script="Encuesta", new_name="Encuesta v2", target_org="cliente", org="lab"))
    assert plan["status"] == "bloqueado" and "solo lectura" in plan["problemas"][0]
    assert len(plan["org"]) == 2


# -- api_call --------------------------------------------------------------

SWAGGER = {
    "paths": {
        "/api/v2/routing/queues": {
            "post": {"parameters": [{"in": "body", "name": "body", "required": True, "schema": {"$ref": "#/definitions/QueueRequest"}}]},
            "get": {"parameters": []},
        },
    },
    "definitions": {"QueueRequest": {"required": ["name"], "properties": {"name": {"type": "string"}}}},
}


@pytest.fixture
def swagger(fake):
    path = fake.tmp_path / "cache" / "swagger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(SWAGGER), encoding="utf-8")
    os.utime(path, None)
    api._swagger = api._index = None
    yield
    api._swagger = api._index = None


def test_api_call_validates_required_fields(fake, swagger):
    plan = run(api.api_call(method="POST", path="/api/v2/routing/queues", body={"description": "x"}, org="lab"))
    assert plan["status"] == "bloqueado" and "campo 'name'" in plan["problemas"][0]
    plan = run(api.api_call(method="POST", path="/api/v2/routing/queues", org="lab"))
    assert plan["status"] == "bloqueado" and "body" in plan["problemas"][0]
    plan = run(api.api_call(method="POST", path="/api/v2/routing/queues", body={"name": "Q"}, org="lab"))
    assert plan["status"] == "requiere_confirmacion"
    plan = run(api.api_call(method="DELETE", path="/api/v2/no/documentado", org="lab"))
    assert plan["status"] == "requiere_confirmacion" and "swagger" in plan["advertencias"][-2]
    assert writes(fake) == []


def test_api_call_get_needs_no_confirmation(fake, swagger):
    fake.on("GET", "/api/v2/routing/queues", json_body=listing({"id": "q1"}))
    out = run(api.api_call(method="GET", path="/api/v2/routing/queues", org="lab"))
    assert out["status"] == "sin_cambios" and out["resultado"]["result"]["entities"] == [{"id": "q1"}]

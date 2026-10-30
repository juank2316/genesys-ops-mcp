"""Clone a script together with the data actions it calls.

A script calls a data action through `bridge.bridge` with `actionName: "custom_-_<uuid>"`. Those ids belong to the
source org, so copying the script alone leaves it pointing at actions that do not exist in the target. This tool:

  1. exports the script and collects every `custom_-_<uuid>` it references,
  2. reads each action from the source (GET ...?expand=contract&includeConfig=true, templates by URI),
  3. creates it in the target (POST /api/v2/integrations/actions, then publishes the draft if there is one),
  4. rewrites the ids in the exported script and imports it (same path as `clone_script`).

Integration credentials are never copied: the target integrations must already be configured.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from pydantic import Field

from ..app import WRITE, ConfirmArg, OrgArg, client, confirmable, mcp
from ..client import GenesysAPIError, GenesysClient, GenesysError
from ..confirm import planning, warn
from .scripts import ScriptArg, _export, _import, _script_id

ACTIONS = "/api/v2/integrations/actions"
INTEGRATIONS = "/api/v2/integrations"
TOOL = "clone_script_with_data_actions"
_ACTION_ID = re.compile(r"custom_-_[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_SECRET_HEADER = re.compile(r"key|secret|token|password|auth", re.I)
_UNSUPPORTED_TYPES = {"function-data-actions"}  # POST /actions does not support them


async def _read_action(c: GenesysClient, action_id: str) -> dict | None:
    try:
        return await c.get(f"{ACTIONS}/{action_id}", expand="contract", includeConfig="true")
    except GenesysAPIError as exc:
        if exc.status == 404:
            return None
        raise


async def _template(c: GenesysClient, uri: str | None) -> str | None:
    if not uri:
        return None
    body = await c.get(uri)
    if body is None:
        return ""
    return body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)  # a JSON-looking template got parsed


def _pick_integration(source: dict, targets: list[dict], integration_map: dict[str, str]) -> dict:
    """Target integration for an action: explicit map, else same name, else the only one of the same type."""
    wanted = integration_map.get(source["name"]) or integration_map.get(source["id"])
    if wanted:
        found = [t for t in targets if wanted in (t["id"], t["name"])]
        if not found:
            raise GenesysError(f"integration_map: no existe la integración {wanted!r} en la org destino.")
        return found[0]
    same_name = [t for t in targets if t["name"] == source["name"]]
    active = [t for t in same_name if (t.get("reportedState") or {}).get("code") == "ACTIVE"]
    if len(active or same_name) == 1:
        return (active or same_name)[0]
    kind = (source.get("integrationType") or {}).get("id")
    same_type = [t for t in targets if (t.get("integrationType") or {}).get("id") == kind]
    if not same_name and len(same_type) == 1:
        warn(
            f"La integración {source['name']!r} no existe en el destino; sus data actions irán a "
            f"{same_type[0]['name']!r} (mismo tipo). Usá integration_map para elegir otra."
        )
        return same_type[0]
    options = ", ".join(sorted(t["name"] for t in (same_name or same_type))) or "ninguna del mismo tipo"
    raise GenesysError(
        f"No puedo elegir la integración destino para {source['name']!r}. Candidatas: {options}. "
        "Indicala con integration_map: {'<origen>': '<destino>'}."
    )


def _create_body(action: dict, integration_id: str, request_template: str | None, success_template: str | None) -> dict:
    cfg = action.get("config") or {}
    contract = action.get("contract") or {}
    try:
        input_schema = contract["input"]["inputSchema"]
        success_schema = contract["output"]["successSchema"]
    except KeyError as exc:
        raise GenesysError(f"El data action {action.get('name')!r} no trae el contrato completo ({exc}).") from exc
    request = {
        k: v for k, v in (cfg.get("request") or {}).items() if k in ("requestUrlTemplate", "requestType", "headers") and v
    }
    if request_template is not None:
        request["requestTemplate"] = request_template
    response_cfg = cfg.get("response") or {}
    response = {k: response_cfg[k] for k in ("translationMap", "translationMapDefaults") if k in response_cfg}
    if success_template is not None:
        response["successTemplate"] = success_template
    config: dict[str, Any] = {"request": request, "response": response}
    if cfg.get("timeoutSeconds"):
        config["timeoutSeconds"] = cfg["timeoutSeconds"]
    return {
        "name": action["name"],
        "category": action.get("category") or "",
        "integrationId": integration_id,
        "secure": bool(action.get("secure")),
        "config": config,
        "contract": {"input": {"inputSchema": input_schema}, "output": {"successSchema": success_schema}},
    }


def _literal_secret_headers(body: dict) -> list[str]:
    headers = ((body.get("config") or {}).get("request") or {}).get("headers") or {}
    return [k for k, v in headers.items() if _SECRET_HEADER.search(k) and isinstance(v, str) and "${" not in v]


async def _publish_if_draft(c: GenesysClient, action_id: str) -> None:
    try:
        draft = await c.get(f"{ACTIONS}/{action_id}/draft")
    except GenesysAPIError as exc:
        if exc.status == 404:  # created already published
            return
        raise
    if (version := (draft or {}).get("version")) is not None:
        await c.request("POST", f"{ACTIONS}/{action_id}/draft/publish", json_body={"version": version}, tool=TOOL)


@mcp.tool(annotations=WRITE)
@confirmable
async def clone_script_with_data_actions(
    script: ScriptArg,
    new_name: Annotated[str, Field(description="Nombre del script nuevo en la org destino.")],
    org: OrgArg,
    target_org: Annotated[str, Field(description="Org destino (perfil o alias). `org` es el origen.")],
    division: Annotated[str | None, Field(description="División destino del script en la org destino.")] = None,
    publish: bool = True,
    integration_map: Annotated[
        dict[str, str],
        Field(description="Integración origen (nombre o id) -> integración destino (nombre o id), si no coinciden por nombre."),
    ] = {},
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Copia un script a otra org junto con los data actions que invoca, y deja el script apuntando a los nuevos.

    Los data actions que ya existan en el destino (misma integración y nombre) se reutilizan. Las referencias a data
    actions que no existen en el origen se dejan tal cual (quedan rotas, igual que en el origen). Las credenciales de las
    integraciones NO se copian: las integraciones destino deben estar configuradas. No soporta Function Data Actions.
    """
    src, dst = client(org), client(target_org)
    if src.profile.name == dst.profile.name:
        raise GenesysError("El origen y el destino son la misma org (usá clone_script).")
    script_id = await _script_id(src, script)
    meta, content = await _export(src, script_id)
    text = content.decode("utf-8")
    action_ids = list(dict.fromkeys(_ACTION_ID.findall(text)))

    src_integrations = {i["id"]: i for i in await src.paginate(INTEGRATIONS)}
    dst_integrations = await dst.paginate(INTEGRATIONS)
    dst_actions = {(a.get("integrationId"), (a.get("name") or "").lower()): a["id"] for a in await dst.paginate(ACTIONS)}

    report: list[dict] = []
    missing: list[str] = []
    secrets: dict[str, list[str]] = {}
    for old_id in action_ids:
        action = await _read_action(src, old_id)
        if action is None:
            missing.append(old_id)
            continue
        source_integration = src_integrations.get(action.get("integrationId"))
        if source_integration is None:
            raise GenesysError(f"No encuentro la integración {action.get('integrationId')} del data action {action['name']!r}.")
        if (source_integration.get("integrationType") or {}).get("id") in _UNSUPPORTED_TYPES:
            raise GenesysError(
                f"El data action {action['name']!r} es de tipo Function: la API no permite crearlo; hay que replicarlo a mano."
            )
        target = _pick_integration(source_integration, dst_integrations, integration_map)
        entry = {"nombre": action["name"], "origen": old_id, "integracion_destino": target["name"]}
        existing = dst_actions.get((target["id"], action["name"].lower()))
        if existing:
            new_id, entry["estado"] = existing, "ya existía (se reutiliza)"
        else:
            request_template = await _template(src, ((action.get("config") or {}).get("request") or {}).get("requestTemplateUri"))
            success_template = await _template(src, ((action.get("config") or {}).get("response") or {}).get("successTemplateUri"))
            body = _create_body(action, target["id"], request_template, success_template)
            if leaked := _literal_secret_headers(body):
                secrets[action["name"]] = leaked
            created = await dst.request(
                "POST",
                ACTIONS,
                json_body=body,
                tool=TOOL,
                plan_preview={"data_action": action["name"], "integracion": target["name"], "categoria": body["category"]},
            )
            new_id, entry["estado"] = created["id"], "creado"
            if not planning():
                await _publish_if_draft(dst, new_id)
        entry["destino"] = new_id
        text = text.replace(old_id, new_id)
        report.append(entry)

    used = sorted({e["integracion_destino"] for e in report})
    inactive = [t["name"] for t in dst_integrations if t["name"] in used and (t.get("reportedState") or {}).get("code") != "ACTIVE"]
    if inactive:
        warn(f"Integraciones destino que no están ACTIVE: {', '.join(inactive)}. Los data actions no funcionarán hasta activarlas.")
    if used:
        warn(f"Las credenciales de las integraciones no se copian: verificá que {', '.join(used)} estén configuradas en {dst.profile.name}.")
    for name, headers in secrets.items():
        warn(f"El data action {name!r} tiene credenciales escritas en los headers ({', '.join(headers)}); se copian tal cual al destino.")
    for old_id in missing:
        warn(f"El data action {old_id} no existe en {src.profile.name}; la referencia queda igual (rota) en el script nuevo.")

    result = await _import(dst, text.encode("utf-8"), new_name, replace_id=None, division=division, publish=publish, tool=TOOL)
    return {
        "source": {"org": src.profile.name, "id": script_id, "name": meta.get("name")},
        "data_actions": report,
        "referencias_sin_origen": missing,
        "target": result,
    }

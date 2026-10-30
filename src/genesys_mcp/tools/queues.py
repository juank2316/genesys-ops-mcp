"""Queues: list, read, create, update, delete, members and wrap-up codes."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from ..app import (
    DESTRUCTIVE,
    READ,
    WRITE,
    ConfirmArg,
    NotFound,
    OrgArg,
    chunks,
    client,
    confirmable,
    deep_merge,
    find_by_name,
    mcp,
    ref,
    resolve_division_id,
    resolve_id,
    resolve_user_id,
    slim,
)
from ..client import GenesysClient, GenesysError
from ..confirm import warn
from .scripts import SCRIPTS, _script_id

QUEUES = "/api/v2/routing/queues"
QueueArg = Annotated[str, Field(description="Id o nombre exacto de la cola.")]
UsersArg = Annotated[list[str], Field(description="Usuarios (ids o emails).", min_length=1)]

_QUEUE_FIELDS = ("id", "name", "description", "division.name", "memberCount", "joinedMemberCount", "skillEvaluationMethod")
_READONLY_QUEUE_KEYS = (
    "memberCount", "userMemberCount", "joinedMemberCount", "dateCreated", "dateModified",
    "modifiedBy", "createdBy", "selfUri",
)


async def _queue_id(c: GenesysClient, queue: str) -> str:
    return await resolve_id(c, QUEUES, queue, "cola")


async def _change_members(c: GenesysClient, queue: str, users: list[str], *, remove: bool) -> dict:
    return await _change_members_by_id(c, await _queue_id(c, queue), users, remove=remove)


async def _change_members_by_id(c: GenesysClient, queue_id: str, users: list[str], *, remove: bool) -> dict:
    user_ids = [await resolve_user_id(c, u) for u in users]
    for batch in chunks(user_ids, 100):
        await c.request(
            "POST",
            f"{QUEUES}/{queue_id}/members",
            params={"delete": "true"} if remove else None,
            json_body=[{"id": uid} for uid in batch],
            tool="remove_queue_members" if remove else "add_queue_members",
        )
    return {"queue_id": queue_id, "removed" if remove else "added": user_ids}


@mcp.tool(annotations=READ)
async def list_queues(
    org: OrgArg,
    name: Annotated[str | None, Field(description="Filtro por nombre (soporta *, p.ej. '*Ventas*').")] = None,
    division: Annotated[str | None, Field(description="Filtrar por división (id o nombre).")] = None,
    max_results: Annotated[int, Field(ge=1, le=5000)] = 200,
) -> dict:
    """Lista colas de ACD con su división y cantidad de miembros."""
    c = client(org)
    params: dict[str, Any] = {"name": name}
    if division:
        params["divisionId"] = await resolve_division_id(c, division)
    items = await c.paginate(QUEUES, params, max_items=max_results)
    return {"org": c.profile.name, "count": len(items), "queues": [slim(q, *_QUEUE_FIELDS) for q in items]}


@mcp.tool(annotations=READ)
async def get_queue(
    queue: QueueArg,
    org: OrgArg,
    include_members: bool = False,
    include_wrapup_codes: bool = True,
) -> dict:
    """Configuración completa de una cola (media settings, ACW, routing, flows, scripts) y opcionalmente miembros y wrap-up codes."""
    c = client(org)
    queue_id = await _queue_id(c, queue)
    out: dict[str, Any] = {"org": c.profile.name, "queue": await c.get(f"{QUEUES}/{queue_id}")}
    if include_wrapup_codes:
        out["wrapup_codes"] = [ref(w) for w in await c.paginate(f"{QUEUES}/{queue_id}/wrapupcodes")]
    if include_members:
        members = await c.paginate(f"{QUEUES}/{queue_id}/members")
        out["members"] = [{**ref(m), "joined": m.get("joined"), "memberBy": m.get("memberBy")} for m in members]
    return out


@mcp.tool(annotations=WRITE)
@confirmable
async def create_queue(
    name: str,
    org: OrgArg,
    description: str | None = None,
    division: Annotated[str | None, Field(description="División (id o nombre). Omitir = Home.")] = None,
    settings: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Campos extra del QueueRequest de Genesys, p.ej. "
                "{'acwSettings': {'wrapupPrompt': 'MANDATORY_TIMEOUT', 'timeoutMs': 30000}, "
                "'skillEvaluationMethod': 'BEST', 'mediaSettings': {'call': {'alertingTimeoutSeconds': 8, "
                "'serviceLevel': {'percentage': 0.8, 'durationMs': 20000}}}, 'enableTranscription': true}. "
                "Usá api_describe('POST', '/api/v2/routing/queues') para ver todos."
            )
        ),
    ] = {},
    wrapup_codes: Annotated[list[str], Field(description="Wrap-up codes a asociar (nombres o ids).")] = [],
    members: Annotated[list[str], Field(description="Usuarios a agregar (ids o emails).")] = [],
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Crea una cola (idempotente por nombre: si ya existe, la devuelve sin modificarla) y opcionalmente le asocia wrap-up codes y miembros."""
    c = client(org)
    try:
        existing = await find_by_name(c, QUEUES, name, "cola")
        return {"org": c.profile.name, "status": "already_existed", "queue": slim(existing, *_QUEUE_FIELDS)}
    except NotFound:
        pass
    body: dict[str, Any] = {**settings, "name": name, "division": {"id": await resolve_division_id(c, division)}}
    if description:
        body["description"] = description
    queue = await c.request("POST", QUEUES, json_body=body, tool="create_queue")
    out: dict[str, Any] = {"org": c.profile.name, "status": "created", "queue": slim(queue, *_QUEUE_FIELDS)}
    if wrapup_codes:
        out["wrapup_codes"] = await _change_wrapups(c, queue["id"], wrapup_codes, "add")
    if members:
        out["members"] = await _change_members_by_id(c, queue["id"], members, remove=False)
    return out


@mcp.tool(annotations=WRITE)
@confirmable
async def update_queue(
    queue: QueueArg,
    org: OrgArg,
    changes: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Cambios a aplicar; se fusionan (deep merge) sobre la configuración actual. "
                "Ej: {'description': 'x', 'acwSettings': {'timeoutMs': 60000}, 'name': 'Nuevo nombre'}."
            )
        ),
    ],
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Modifica la configuración de una cola sin perder lo que no se cambia (lee, fusiona y hace PUT)."""
    c = client(org)
    queue_id = await _queue_id(c, queue)
    current = await c.get(f"{QUEUES}/{queue_id}")
    body = deep_merge({k: v for k, v in current.items() if k not in _READONLY_QUEUE_KEYS}, changes)
    updated = await c.request("PUT", f"{QUEUES}/{queue_id}", json_body=body, tool="update_queue")
    return {"org": c.profile.name, "queue": slim(updated, *_QUEUE_FIELDS), "changed_keys": sorted(changes)}


MediaType = Literal["CALL", "CALLBACK", "CHAT", "EMAIL", "MESSAGE"]
_FEATURE_MEDIA = {"callback": "CALLBACK", "chat": "CHAT", "email": "EMAIL", "message": "MESSAGE"}


def _script_media_types(script: dict) -> list[str]:
    """Media types a script was built for: voice always, the rest when its feature is enabled."""
    features = script.get("features") or {}
    enabled = [
        media
        for feature, media in _FEATURE_MEDIA.items()
        if ((features.get(feature) or {}).get("properties") or {}).get("enabled", {}).get("value") is True
    ]
    return ["CALL", *enabled]


@mcp.tool(annotations=WRITE)
@confirmable
async def set_queue_default_script(
    script: Annotated[str, Field(description="Id o nombre exacto del script (debe estar publicado en esa org).")],
    org: OrgArg,
    queues: Annotated[list[str] | None, Field(description="Colas (ids o nombres exactos).")] = None,
    name_pattern: Annotated[str | None, Field(description="Patrón de nombre de cola (soporta *, p.ej. 'Contigencia*').")] = None,
    all_queues: Annotated[bool, Field(description="Todas las colas de la org.")] = False,
    media_types: Annotated[
        list[MediaType] | None,
        Field(description="Canales donde queda por defecto. Omitir = los que el script tiene habilitados (voz siempre)."),
    ] = None,
    replace_existing: Annotated[
        bool, Field(description="Reemplazar un script por defecto distinto que ya tenga la cola. false = respetarlo y reportarlo.")
    ] = False,
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Deja un script como script por defecto (defaultScripts) en varias colas y canales a la vez.

    Indicá la selección con `queues`, `name_pattern` o `all_queues`. El plan muestra, por cola y canal, el script
    actual y el nuevo. Solo toca `defaultScripts`: el resto de la cola se conserva.
    """
    if sum([bool(queues), bool(name_pattern), all_queues]) != 1:
        raise GenesysError("Indicá exactamente una selección de colas: `queues`, `name_pattern` o `all_queues`.")
    c = client(org)
    script_id = await _script_id(c, script)
    script_data = await c.get(f"{SCRIPTS}/{script_id}")
    supported = _script_media_types(script_data)
    media = list(media_types) if media_types else supported
    if unsupported := [m for m in media if m not in supported]:
        warn(f"El script no tiene habilitado el canal {', '.join(unsupported)}; Genesys puede rechazarlo o no usarlo.")
    if all_queues:
        targets = await c.paginate(QUEUES)
    elif name_pattern:
        targets = await c.paginate(QUEUES, {"name": name_pattern})
    else:
        targets = [{"id": await _queue_id(c, q)} for q in queues]
    script_names = {s["id"]: s.get("name") for s in await c.paginate(SCRIPTS)}

    changed, kept, untouched = [], [], 0
    for target in targets:
        current = await c.get(f"{QUEUES}/{target['id']}")
        existing = {m: (v or {}).get("id") for m, v in (current.get("defaultScripts") or {}).items()}
        changes: dict[str, dict] = {}
        for m in media:
            old = existing.get(m)
            if old == script_id:
                continue
            if old and not replace_existing:
                kept.append({"cola": current["name"], "canal": m, "script_actual": script_names.get(old) or old})
                continue
            changes[m] = {"antes": (script_names.get(old) or old) if old else None, "despues": script_names.get(script_id)}
        if not changes:
            untouched += 1
            continue
        body = deep_merge(
            {k: v for k, v in current.items() if k not in _READONLY_QUEUE_KEYS},
            {"defaultScripts": {m: {"id": script_id} for m in changes}},
        )
        await c.request(
            "PUT",
            f"{QUEUES}/{current['id']}",
            json_body=body,
            tool="set_queue_default_script",
            plan_preview={"cola": current["name"], "defaultScripts": changes},
        )
        changed.append({"cola": current["name"], "cambios": changes})
    if kept:
        warn(
            f"{len(kept)} canal(es) ya tienen otro script por defecto y NO se reemplazan "
            "(usá replace_existing=true para reemplazarlos)."
        )
    return {
        "org": c.profile.name,
        "script": {"id": script_id, "name": script_names.get(script_id)},
        "canales": media,
        "colas_evaluadas": len(targets),
        "colas_a_cambiar": changed,
        "colas_sin_cambios": untouched,
        "respetados_por_script_existente": kept,
    }


@mcp.tool(annotations=DESTRUCTIVE)
@confirmable
async def delete_queue(queue: QueueArg, org: OrgArg, confirmation_token: ConfirmArg = None) -> dict:
    """Elimina una cola. Requiere allow_delete en el perfil."""
    c = client(org)
    queue_id = await _queue_id(c, queue)
    data = await c.get(f"{QUEUES}/{queue_id}")
    await c.request("DELETE", f"{QUEUES}/{queue_id}", tool="delete_queue")
    return {"org": c.profile.name, "deleted": slim(data, "id", "name", "memberCount")}


@mcp.tool(annotations=READ)
async def list_queue_members(
    queue: QueueArg,
    org: OrgArg,
    joined: Annotated[bool | None, Field(description="Solo los que están 'joined' (true) o no (false).")] = None,
) -> dict:
    """Lista los miembros de una cola con su estado joined y routing status."""
    c = client(org)
    queue_id = await _queue_id(c, queue)
    members = await c.paginate(
        f"{QUEUES}/{queue_id}/members", {"joined": joined, "expand": "routingStatus"}
    )
    return {
        "org": c.profile.name,
        "queue_id": queue_id,
        "count": len(members),
        "members": [
            {**ref(m), "joined": m.get("joined"), "memberBy": m.get("memberBy"),
             "routingStatus": (m.get("routingStatus") or {}).get("status")}
            for m in members
        ],
    }


@mcp.tool(annotations=WRITE)
@confirmable
async def add_queue_members(queue: QueueArg, users: UsersArg, org: OrgArg, confirmation_token: ConfirmArg = None) -> dict:
    """Agrega usuarios a una cola (en lotes de 100)."""
    c = client(org)
    return {"org": c.profile.name, **await _change_members(c, queue, users, remove=False)}


@mcp.tool(annotations=WRITE)
@confirmable
async def remove_queue_members(queue: QueueArg, users: UsersArg, org: OrgArg, confirmation_token: ConfirmArg = None) -> dict:
    """Quita usuarios de una cola."""
    c = client(org)
    return {"org": c.profile.name, **await _change_members(c, queue, users, remove=True)}


async def _change_wrapups(c: GenesysClient, queue_id: str, codes: list[str], mode: str) -> dict:
    ids = [await resolve_id(c, "/api/v2/routing/wrapupcodes", code, "wrap-up code") for code in codes]
    if mode == "add":
        await c.request(
            "POST", f"{QUEUES}/{queue_id}/wrapupcodes", json_body=[{"id": i} for i in ids], tool="set_queue_wrapup_codes"
        )
    else:
        for i in ids:
            await c.request("DELETE", f"{QUEUES}/{queue_id}/wrapupcodes/{i}", tool="set_queue_wrapup_codes")
    return {"mode": mode, "ids": ids}


@mcp.tool(annotations=WRITE)
@confirmable
async def set_queue_wrapup_codes(
    queue: QueueArg,
    codes: Annotated[list[str], Field(description="Wrap-up codes (nombres o ids).", min_length=1)],
    org: OrgArg,
    mode: Literal["add", "remove"] = "add",
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Asocia o desasocia wrap-up codes de una cola."""
    c = client(org)
    queue_id = await _queue_id(c, queue)
    result = await _change_wrapups(c, queue_id, codes, mode)
    current = [ref(w) for w in await c.paginate(f"{QUEUES}/{queue_id}/wrapupcodes")]
    return {"org": c.profile.name, "queue_id": queue_id, **result, "current": current}

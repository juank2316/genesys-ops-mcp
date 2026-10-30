"""Skills, languages and wrap-up codes: list and idempotent bulk create."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..app import READ, WRITE, ConfirmArg, OrgArg, client, confirmable, mcp, ref, resolve_division_id
from ..client import GenesysClient, GenesysError
from ..confirm import current_plan, planning

MAX_COPY_ERRORS_SHOWN = 20

NamesArg = Annotated[list[str], Field(description="Nombres a crear. Los que ya existen se omiten.", min_length=1)]
FilterArg = Annotated[str | None, Field(description="Filtro por nombre (soporta * como comodín, p.ej. 'Ventas*').")]

_KINDS = {
    "skill": "/api/v2/routing/skills",
    "language": "/api/v2/routing/languages",
    "wrapup": "/api/v2/routing/wrapupcodes",
}


async def _list(c: GenesysClient, kind: str, name: str | None) -> list[dict]:
    return await c.paginate(_KINDS[kind], {"name": name})


async def _create_missing(c: GenesysClient, kind: str, names: list[str], tool: str, extra: dict | None = None) -> dict:
    existing = {e["name"].lower(): e for e in await _list(c, kind, None) if e.get("name")}
    created, skipped, errors = [], [], []
    for name in dict.fromkeys(n.strip() for n in names if n.strip()):
        if name.lower() in existing:
            skipped.append(ref(existing[name.lower()]))
            continue
        try:
            body = {"name": name, **(extra or {})}
            created.append(ref(await c.request("POST", _KINDS[kind], json_body=body, tool=tool)))
        except GenesysError as exc:
            errors.append({"name": name, "error": str(exc)})
    return {"org": c.profile.name, "created": created, "already_existed": skipped, "errors": errors}


@mcp.tool(annotations=READ)
async def list_skills(org: OrgArg, name: FilterArg = None) -> dict:
    """Lista los ACD skills de la org."""
    c = client(org)
    items = await _list(c, "skill", name)
    return {"org": c.profile.name, "count": len(items), "skills": [ref(i) for i in items]}


@mcp.tool(annotations=WRITE)
@confirmable
async def create_skills(names: NamesArg, org: OrgArg, confirmation_token: ConfirmArg = None) -> dict:
    """Crea ACD skills (idempotente: omite los que ya existen con ese nombre)."""
    return await _create_missing(client(org), "skill", names, "create_skills")


@mcp.tool(annotations=READ)
async def list_languages(org: OrgArg, name: FilterArg = None) -> dict:
    """Lista los idiomas de routing (routing languages) de la org."""
    c = client(org)
    items = await _list(c, "language", name)
    return {"org": c.profile.name, "count": len(items), "languages": [ref(i) for i in items]}


@mcp.tool(annotations=WRITE)
@confirmable
async def create_languages(names: NamesArg, org: OrgArg, confirmation_token: ConfirmArg = None) -> dict:
    """Crea idiomas de routing (idempotente)."""
    return await _create_missing(client(org), "language", names, "create_languages")


@mcp.tool(annotations=READ)
async def list_wrapup_codes(org: OrgArg, name: FilterArg = None) -> dict:
    """Lista los códigos de wrap-up (tipificaciones) de la org."""
    c = client(org)
    items = await _list(c, "wrapup", name)
    return {"org": c.profile.name, "count": len(items), "wrapup_codes": [ref(i) for i in items]}


@mcp.tool(annotations=WRITE)
@confirmable
async def create_wrapup_codes(
    names: NamesArg,
    org: OrgArg,
    division: Annotated[str | None, Field(description="División (id o nombre). Omitir = Home.")] = None,
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Crea códigos de wrap-up (idempotente)."""
    c = client(org)
    division_id = await resolve_division_id(c, division)
    return await _create_missing(c, "wrapup", names, "create_wrapup_codes", {"division": {"id": division_id}})


@mcp.tool(annotations=WRITE)
@confirmable
async def copy_wrapup_codes(
    org: OrgArg,
    target_org: Annotated[str, Field(description="Org destino (perfil o alias). `org` es el origen.")],
    name: FilterArg = None,
    division: Annotated[str | None, Field(description="División en la org destino (id o nombre). Omitir = Home.")] = None,
    max_create: Annotated[
        int, Field(ge=1, le=2000, description="Máximo de códigos a crear en esta llamada; repetir hasta que `pendientes` sea 0.")
    ] = 500,
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Copia los códigos de wrap-up de una org a otra (idempotente: omite los que ya existen por nombre).

    Lee ambas orgs dentro del servidor, así que no hay que pasar los nombres por el chat. El plan y el resultado son
    resúmenes (cantidades y muestras). Solo copia nombres: no replica la división de origen ni referencias en colas.
    """
    src, dst = client(org), client(target_org)
    if src.profile.name == dst.profile.name:
        raise GenesysError("El origen y el destino son la misma org.")
    wanted: dict[str, str] = {}  # Genesys compares names case-insensitively; keep the first spelling
    for e in await _list(src, "wrapup", name):
        if e.get("name", "").strip():
            wanted.setdefault(e["name"].strip().lower(), e["name"].strip())
    existing = {e["name"].lower() for e in await _list(dst, "wrapup", None) if e.get("name")}
    missing = [n for key, n in wanted.items() if key not in existing]
    batch = missing[:max_create]
    division_id = await resolve_division_id(dst, division)
    summary: dict = {
        "origen": src.profile.name,
        "destino": dst.profile.name,
        "en_origen": len(wanted),
        "ya_existian_en_destino": len(wanted) - len(missing),
        "a_crear_ahora": len(batch),
        "pendientes_despues": len(missing) - len(batch),
    }
    if planning():
        if batch:
            dst._check_policy("POST", _KINDS["wrapup"])  # the plan bypasses request(), so enforce read_only here
            current_plan().record(
                dst.profile.name,
                "POST",
                _KINDS["wrapup"],
                None,
                None,
                preview={"crear": len(batch), "primeros": batch[:5], "ultimos": batch[-3:], "division_id": division_id},
            )
        return summary
    created, errors = 0, []
    for wrapup in batch:
        try:
            await dst.request(
                "POST", _KINDS["wrapup"], json_body={"name": wrapup, "division": {"id": division_id}}, tool="copy_wrapup_codes"
            )
            created += 1
        except GenesysError as exc:
            errors.append({"name": wrapup, "error": str(exc)})
    summary["creados"] = created
    summary["pendientes"] = len(missing) - created
    summary["errores"] = len(errors)
    if errors:
        summary["errores_muestra"] = errors[:MAX_COPY_ERRORS_SHOWN]
    return summary

"""Agent scripts.

The public API only reads/exports/publishes scripts. Creating or replacing one
goes through the upload service used by the Genesys Terraform provider
(MyPureCloud/terraform-provider-genesyscloud, genesyscloud/scripts):

  POST /uploads/v2/scripter  multipart(file, scriptName[, scriptIdToReplace]) -> {correlationId}
  GET  /api/v2/scripts/uploads/{correlationId}/status                        -> {succeeded, message}
  POST /api/v2/authorization/divisions/{divisionId}/objects/SCRIPT  [scriptId]
  POST /api/v2/scripts/published {scriptId}
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from ..app import (
    DESTRUCTIVE,
    READ,
    WRITE,
    ConfirmArg,
    OrgArg,
    client,
    confirmable,
    mcp,
    resolve_division_id,
    resolve_id,
    settings,
    slim,
)
from ..client import GenesysClient, GenesysError
from ..confirm import PLACEHOLDER_ID, planning

SCRIPTS = "/api/v2/scripts"
ScriptArg = Annotated[str, Field(description="Id o nombre exacto del script.")]
_SCRIPT_FIELDS = ("id", "name", "division.name", "publishedDate", "modifiedDate", "versionId", "createdDate")
_UPLOAD_TIMEOUT = 90.0
_UPLOAD_POLL_SECONDS = 2.0


async def _script_id(c: GenesysClient, script: str) -> str:
    return await resolve_id(c, SCRIPTS, script, "script")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "script"


@mcp.tool(annotations=READ)
async def list_scripts(
    org: OrgArg,
    name: Annotated[str | None, Field(description="Filtro por nombre.")] = None,
    published_only: bool = False,
    max_results: Annotated[int, Field(ge=1, le=2000)] = 200,
) -> dict:
    """Lista los scripts de agente (borradores o solo publicados)."""
    c = client(org)
    path = f"{SCRIPTS}/published" if published_only else SCRIPTS
    items = await c.paginate(path, {"name": name, "sortBy": "modifiedDate", "sortOrder": "descending"}, max_items=max_results)
    return {"org": c.profile.name, "count": len(items), "scripts": [slim(s, *_SCRIPT_FIELDS) for s in items]}


@mcp.tool(annotations=READ)
async def get_script(script: ScriptArg, org: OrgArg, include_pages: bool = True) -> dict:
    """Detalle de un script: metadatos, variables y (opcional) la lista de páginas."""
    c = client(org)
    script_id = await _script_id(c, script)
    data = await c.get(f"{SCRIPTS}/{script_id}")
    out: dict[str, Any] = {"org": c.profile.name, "script": slim(data, *_SCRIPT_FIELDS, "features", "variables")}
    if include_pages:
        pages = await c.get(f"{SCRIPTS}/{script_id}/pages")
        out["pages"] = [slim(p, "id", "name") for p in (pages if isinstance(pages, list) else [])]
    return out


async def _export(c: GenesysClient, script_id: str) -> tuple[dict, bytes]:
    meta = await c.get(f"{SCRIPTS}/{script_id}")
    resp = await c.request("POST", f"{SCRIPTS}/{script_id}/export", json_body={})
    url = (resp or {}).get("url")
    if not url:
        raise GenesysError(f"Genesys no devolvió URL de exportación para el script {script_id}.")
    return meta, await c.download(url)


@mcp.tool(annotations=READ)
async def export_script(
    script: ScriptArg,
    org: OrgArg,
    output_path: Annotated[str | None, Field(description="Ruta del archivo destino. Omitir = carpeta exports/ del MCP.")] = None,
) -> dict:
    """Exporta un script a un archivo JSON local (sirve de respaldo, plantilla o para migrarlo a otra org)."""
    c = client(org)
    script_id = await _script_id(c, script)
    meta, content = await _export(c, script_id)
    if output_path:
        path = Path(output_path).expanduser()
    else:
        path = settings().data_dir / "exports" / f"{_safe_name(meta['name'])}__{c.profile.name}.script.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"org": c.profile.name, "script": slim(meta, "id", "name"), "file": str(path.resolve()), "bytes": len(content)}


async def _wait_upload(c: GenesysClient, correlation_id: str) -> dict:
    deadline = time.monotonic() + _UPLOAD_TIMEOUT
    last: dict = {}
    while time.monotonic() < deadline:
        last = await c.get(f"{SCRIPTS}/uploads/{correlation_id}/status", longPoll="true") or {}
        if last.get("succeeded"):
            return last
        await asyncio.sleep(_UPLOAD_POLL_SECONDS)
    raise GenesysError(f"La importación del script no terminó en {int(_UPLOAD_TIMEOUT)}s (status: {last}).")


async def _find_new_script(c: GenesysClient, name: str) -> dict:
    items = await c.paginate(SCRIPTS, {"name": name, "sortBy": "modifiedDate", "sortOrder": "descending"})
    matches = [s for s in items if s.get("name") == name]
    if not matches:
        raise GenesysError(f"El upload terminó pero no encuentro el script {name!r}.")
    return matches[0]


async def _import(
    c: GenesysClient,
    content: bytes,
    script_name: str,
    *,
    replace_id: str | None,
    division: str | None,
    publish: bool,
    tool: str,
) -> dict:
    form = {"scriptName": script_name}
    if replace_id:
        form["scriptIdToReplace"] = replace_id
    files = {"file": (f"{_safe_name(script_name)}.script.json", content, "application/json")}
    resp = await c.request("POST", f"{c.profile.apps_url}/uploads/v2/scripter", files=files, data=form, tool=tool)
    correlation_id = (resp or {}).get("correlationId")
    if not correlation_id:
        raise GenesysError(f"El servicio de upload no devolvió correlationId: {resp}")
    if planning():
        script = {"id": replace_id or PLACEHOLDER_ID, "name": script_name}
    else:
        await _wait_upload(c, correlation_id)
        script = await c.get(f"{SCRIPTS}/{replace_id}") if replace_id else await _find_new_script(c, script_name)
    out: dict[str, Any] = {"org": c.profile.name, "script": slim(script, "id", "name"), "replaced": bool(replace_id)}
    if division:
        division_id = await resolve_division_id(c, division)
        await c.request(
            "POST", f"/api/v2/authorization/divisions/{division_id}/objects/SCRIPT", json_body=[script["id"]], tool=tool
        )
        out["division_id"] = division_id
    if publish:
        await c.request("POST", f"{SCRIPTS}/published", json_body={"scriptId": script["id"]}, tool=tool)
        out["published"] = True
    return out


def _apply_substitutions(content: bytes, substitutions: dict[str, str]) -> bytes:
    if not substitutions:
        return content
    text = content.decode("utf-8")
    for key, value in substitutions.items():
        text = text.replace("{{" + key + "}}", value)
    return text.encode("utf-8")


@mcp.tool(annotations=WRITE)
@confirmable
async def import_script(
    file_path: Annotated[str, Field(description="Archivo JSON del script (exportado de Genesys o plantilla).")],
    script_name: Annotated[str, Field(description="Nombre que tendrá el script en la org.")],
    org: OrgArg,
    replace_script: Annotated[str | None, Field(description="Id o nombre de un script existente a reemplazar (update).")] = None,
    division: Annotated[str | None, Field(description="División destino (id o nombre). Omitir = la que asigne Genesys (Home).")] = None,
    publish: bool = True,
    substitutions: Annotated[
        dict[str, str], Field(description="Reemplazos de texto {{clave}} -> valor aplicados al archivo antes de subirlo.")
    ] = {},
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Crea o reemplaza un script a partir de un archivo JSON y opcionalmente lo publica."""
    c = client(org)
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise GenesysError(f"No existe el archivo {path}.")
    content = _apply_substitutions(path.read_bytes(), substitutions)
    try:
        json.loads(content)
    except ValueError as exc:
        raise GenesysError(f"{path} no es JSON válido: {exc}") from exc
    replace_id = await _script_id(c, replace_script) if replace_script else None
    return await _import(c, content, script_name, replace_id=replace_id, division=division, publish=publish, tool="import_script")


@mcp.tool(annotations=WRITE)
@confirmable
async def clone_script(
    script: ScriptArg,
    new_name: Annotated[str, Field(description="Nombre del script nuevo.")],
    org: OrgArg,
    target_org: Annotated[str | None, Field(description="Org destino (perfil). Omitir = misma org que el origen.")] = None,
    division: Annotated[str | None, Field(description="División destino en la org destino.")] = None,
    publish: bool = True,
    substitutions: Annotated[dict[str, str], Field(description="Reemplazos {{clave}} -> valor.")] = {},
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Clona un script dentro de la misma org o lo copia a otra org (export + import).

    Ojo al copiar entre orgs: referencias a colas, data actions o flows dentro del script apuntan a ids de la org
    origen y hay que revisarlas en la org destino.
    """
    src = client(org)
    dst = client(target_org) if target_org else src
    script_id = await _script_id(src, script)
    meta, content = await _export(src, script_id)
    content = _apply_substitutions(content, substitutions)
    result = await _import(dst, content, new_name, replace_id=None, division=division, publish=publish, tool="clone_script")
    return {"source": {"org": src.profile.name, "id": script_id, "name": meta.get("name")}, "target": result}


@mcp.tool(annotations=WRITE)
@confirmable
async def publish_script(script: ScriptArg, org: OrgArg, confirmation_token: ConfirmArg = None) -> dict:
    """Publica la versión actual (borrador) de un script."""
    c = client(org)
    script_id = await _script_id(c, script)
    data = await c.request("POST", f"{SCRIPTS}/published", json_body={"scriptId": script_id}, tool="publish_script")
    return {"org": c.profile.name, "published": slim(data, "id", "name", "publishedDate", "versionId")}


@mcp.tool(annotations=DESTRUCTIVE)
@confirmable
async def delete_script(script: ScriptArg, org: OrgArg, confirmation_token: ConfirmArg = None) -> dict:
    """Elimina un script (endpoint no documentado en el swagger, el mismo que usa Terraform). Requiere allow_delete."""
    c = client(org)
    script_id = await _script_id(c, script)
    meta = await c.get(f"{SCRIPTS}/{script_id}")
    await c.request("DELETE", f"{SCRIPTS}/{script_id}", tool="delete_script")
    return {"org": c.profile.name, "deleted": slim(meta, "id", "name")}

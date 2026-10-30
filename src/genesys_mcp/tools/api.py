"""Generic access to the whole Platform API, driven by the official swagger.

api_search -> api_describe -> api_call covers the ~3,300 operations that have no dedicated tool.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from ..app import READ, WRITE, ConfirmArg, OrgArg, client, confirmable, mcp, settings
from ..client import GenesysClient, GenesysError, is_write
from ..confirm import planning, warn

_METHODS = ("get", "post", "put", "patch", "delete")
_CACHE_TTL = 7 * 24 * 3600
_MAX_RESPONSE_CHARS = 60_000

_swagger: dict | None = None
_index: list[dict] | None = None


async def _load_swagger(c: GenesysClient) -> dict:
    global _swagger
    if _swagger is not None:
        return _swagger
    path = settings().data_dir / "cache" / "swagger.json"
    if not path.is_file() or time.time() - path.stat().st_mtime > _CACHE_TTL:
        content = await c.download(f"{c.profile.api_url}/api/v2/docs/swagger")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _swagger = json.loads(path.read_text(encoding="utf-8"))
    return _swagger


async def _load_index(c: GenesysClient) -> list[dict]:
    global _index
    if _index is None:
        spec = await _load_swagger(c)
        _index = []
        for p, ops in spec["paths"].items():
            for m, op in ops.items():
                if m not in _METHODS:
                    continue
                perms = (op.get("x-inin-requires-permissions") or {}).get("permissions") or []
                _index.append(
                    {
                        "method": m.upper(),
                        "path": p,
                        "summary": op.get("summary", ""),
                        "tag": (op.get("tags") or [""])[0],
                        "operationId": op.get("operationId", ""),
                        "permissions": perms,
                        "deprecated": op.get("deprecated", False),
                        "_text": f"{p} {op.get('summary', '')} {' '.join(op.get('tags') or [])} {op.get('operationId', '')}".lower(),
                    }
                )
    return _index


def _score(entry: dict, terms: list[str]) -> int:
    score = 0
    for t in terms:
        if t in entry["path"].lower():
            score += 3
        elif t in entry["_text"]:
            score += 1
        else:
            return 0
    return score - (5 if entry["deprecated"] else 0)


@mcp.tool(annotations=READ)
async def api_search(
    query: Annotated[str, Field(description="Palabras clave en inglés, p.ej. 'queue estimated wait', 'flow publish', 'outbound campaign'.")],
    org: OrgArg,
    method: Annotated[Literal["GET", "POST", "PUT", "PATCH", "DELETE"] | None, Field(description="Filtrar por método.")] = None,
    max_results: Annotated[int, Field(ge=1, le=50)] = 15,
) -> dict:
    """Busca endpoints de la Platform API de Genesys Cloud (swagger oficial) por palabras clave.

    Úsalo cuando no hay una tool dedicada. Luego api_describe para ver parámetros y body, y api_call para ejecutarlo.
    """
    index = await _load_index(client(org))
    terms = [t for t in re.split(r"\s+", query.lower().strip()) if t]
    scored = [(e, _score(e, terms)) for e in index if not method or e["method"] == method]
    hits = sorted((x for x in scored if x[1] > 0), key=lambda x: (-x[1], len(x[0]["path"])))[:max_results]
    return {
        "count": len(hits),
        "results": [{k: v for k, v in e.items() if not k.startswith("_")} for e, _ in hits],
    }


def _resolve(spec: dict, schema: dict | None, depth: int) -> Any:
    if not schema:
        return None
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if depth <= 0:
            return f"<{name}>"
        return {"$model": name, **(_resolve(spec, spec["definitions"].get(name, {}), depth) or {})}
    if schema.get("type") == "array":
        return [_resolve(spec, schema.get("items"), depth)]
    if "properties" in schema:
        required = set(schema.get("required") or [])
        props = {}
        for k, v in schema["properties"].items():
            if v.get("readOnly"):
                continue
            desc = (v.get("description") or "")[:120]
            if "$ref" in v or v.get("type") == "array":
                val = _resolve(spec, v, depth - 1)
            else:
                val = v.get("type", "object")
                if v.get("enum"):
                    val = f"{val} enum{v['enum'][:12]}"
            props[k + ("*" if k in required else "")] = {"type": val, "desc": desc} if desc else val
        return props
    return schema.get("type", "object")


@mcp.tool(annotations=READ)
async def api_describe(
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
    path: Annotated[str, Field(description="Path tal como aparece en api_search, p.ej. /api/v2/routing/queues/{queueId}.")],
    org: OrgArg,
    schema_depth: Annotated[int, Field(ge=1, le=4, description="Profundidad para expandir modelos anidados.")] = 2,
) -> dict:
    """Describe un endpoint: parámetros (path/query), body esperado (campos con * son obligatorios), permisos y respuesta."""
    spec = await _load_swagger(client(org))
    op = spec["paths"].get(path, {}).get(method.lower())
    if not op:
        raise GenesysError(f"No existe {method} {path} en el swagger. Usá api_search.")
    params, body = [], None
    for p in op.get("parameters", []):
        if p.get("in") == "body":
            body = _resolve(spec, p.get("schema"), schema_depth)
            continue
        params.append(
            {
                "name": p["name"],
                "in": p["in"],
                "required": p.get("required", False),
                "type": p.get("type") or (p.get("items") or {}).get("type"),
                "enum": p.get("enum") or (p.get("items") or {}).get("enum"),
                "description": (p.get("description") or "")[:200],
            }
        )
    ok = next((r for code, r in op.get("responses", {}).items() if code.startswith("2")), {})
    return {
        "method": method,
        "path": path,
        "summary": op.get("summary"),
        "description": (op.get("description") or "")[:800],
        "permissions": (op.get("x-inin-requires-permissions") or {}),
        "parameters": params,
        "body": body,
        "response": _resolve(spec, ok.get("schema"), 1),
        "deprecated": op.get("deprecated", False),
    }


PathArg = Annotated[str, Field(description="Path real con ids ya reemplazados, p.ej. /api/v2/routing/queues/abc-123.")]
QueryArg = Annotated[dict[str, Any], Field(description="Parámetros de query string.")]
SaveArg = Annotated[str | None, Field(description="Guardar la respuesta completa en este archivo JSON (para respuestas grandes).")]


@mcp.tool(annotations=READ)
async def api_get(
    path: PathArg,
    org: OrgArg,
    query: QueryArg = {},
    fetch_all_pages: Annotated[bool, Field(description="Juntar todas las páginas de 'entities'.")] = False,
    max_items: Annotated[int, Field(ge=1, le=20000)] = 1000,
    save_to: SaveArg = None,
) -> dict:
    """GET de solo lectura a cualquier endpoint de la Platform API. Respuestas muy grandes se truncan: usá save_to."""
    return await _call("GET", path, query, None, fetch_all_pages, max_items, save_to, org)


@mcp.tool(annotations=WRITE)
@confirmable
async def api_call(
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
    path: PathArg,
    org: OrgArg,
    query: QueryArg = {},
    body: Annotated[Any, Field(description="Body JSON (objeto o lista) para POST/PUT/PATCH.")] = None,
    save_to: SaveArg = None,
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Llama a cualquier endpoint de la Platform API, incluidas escrituras (respeta read_only / allow_delete del perfil).

    Usá api_describe antes para armar el body correctamente. Para lecturas preferí api_get.
    """
    return await _call(method, path, query, body, False, 1000, save_to, org)


def _match_operation(spec: dict, method: str, path: str) -> tuple[str, dict] | None:
    """Find the swagger operation for a concrete path; the template with fewest placeholders wins."""
    candidates = []
    for template, ops in spec["paths"].items():
        op = ops.get(method.lower())
        if op is not None and re.fullmatch(re.sub(r"\\{[^}]+\\}", "[^/]+", re.escape(template)), path):
            candidates.append((template.count("{"), template, op))
    if not candidates:
        return None
    _, template, op = min(candidates, key=lambda x: x[0])
    return template, op


async def _validate_write(c: GenesysClient, method: str, path: str, query: dict, body: Any) -> None:
    """Plan-mode check of a generic write against the swagger: missing required params/fields block the plan."""
    spec = await _load_swagger(c)
    match = _match_operation(spec, method, path)
    if match is None:
        warn(f"{method} {path} no figura en el swagger: no se pudieron validar parámetros ni body.")
        return
    template, op = match
    if op.get("deprecated"):
        warn(f"{method} {template} está deprecado en la API de Genesys.")
    missing = []
    for p in op.get("parameters", []):
        if p.get("in") == "query" and p.get("required") and p["name"] not in (query or {}):
            missing.append(f"parámetro de query '{p['name']}'")
        if p.get("in") == "body":
            if body is None:
                if p.get("required"):
                    missing.append("body")
                continue
            schema = p.get("schema") or {}
            if "$ref" in schema and isinstance(body, dict):
                model = spec["definitions"].get(schema["$ref"].rsplit("/", 1)[-1], {})
                missing += [f"campo '{f}' del body" for f in model.get("required", []) if f not in body]
    if missing:
        raise GenesysError(
            f"Faltan datos obligatorios para {method} {template}: {', '.join(missing)}. "
            f"Usá api_describe('{method}', '{template}') para ver el detalle."
        )


async def _call(method, path, query, body, fetch_all_pages, max_items, save_to, org) -> dict:
    if not path.startswith("/api/v2/"):
        raise GenesysError("El path debe empezar con /api/v2/.")
    if re.search(r"\{[^}]+\}", path):
        raise GenesysError(f"Reemplazá los placeholders del path por valores reales: {path}")
    c = client(org)
    if planning() and is_write(method, path):
        await _validate_write(c, method, path, query, body)
    if method == "GET" and fetch_all_pages:
        result: Any = await c.paginate(path, query, max_items=max_items)
    else:
        result = await c.request(method, path, params=query, json_body=body, tool="api_call")
    out: dict[str, Any] = {"org": c.profile.name, "method": method, "path": path}
    text = json.dumps(result, ensure_ascii=False, default=str)
    if save_to:
        target = Path(save_to).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        out["saved_to"] = str(target.resolve())
    if len(text) > _MAX_RESPONSE_CHARS:
        out["truncated"] = True
        out["total_chars"] = len(text)
        out["result_preview"] = text[:_MAX_RESPONSE_CHARS]
        if not save_to:
            out["hint"] = "Respuesta truncada: repetí con save_to para obtenerla completa, o filtrá con query."
    else:
        out["result"] = result
    return out

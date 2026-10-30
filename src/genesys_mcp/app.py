"""Shared MCPServer instance, per-org clients and lookup helpers used by the tool modules."""

from __future__ import annotations

import re
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .client import AuditLog, GenesysClient, GenesysError
from .config import Settings, load_settings
from .confirm import ConfirmArg, confirmable  # noqa: F401  (re-exported for the tool modules)

INSTRUCTIONS = """\
Servidor para administrar orgs de Genesys Cloud (usuarios, colas, skills, idiomas, wrap-up codes, scripts, troncales)
y llamar cualquier endpoint de la Platform API.

Reglas:
- Cada org es un cliente. `org` es OBLIGATORIO en todas las tools (nombre del perfil o un alias; ver `list_orgs`).
  No hay org por defecto: si el usuario no dijo sobre qué cliente actuar, preguntale. Nunca lo supongas ni lo
  arrastres de un pedido anterior sin que el usuario lo repita. Mencioná la org (display_name y ambiente) en cada
  respuesta.
- Toda tool que cambia o borra algo funciona en dos pasos:
  1. Llamala sin `confirmation_token`: NO cambia nada; valida los datos y devuelve el plan.
     - status "bloqueado": faltan datos o algo no existe. Explicale los problemas al usuario y pedile lo que falta.
     - status "sin_cambios": no hay nada que hacer (p.ej. ya existía).
     - status "requiere_confirmacion": mostrale al usuario la org, la acción, los cambios planeados y las
       advertencias, y preguntale si confirma.
  2. Solo si el usuario responde que sí, repetí la MISMA llamada (mismos argumentos) con el `confirmation_token`.
  Nunca uses un token sin la aprobación explícita del usuario para ese plan, y nunca reutilices un token.
- Antes de planificar, pedile al usuario los datos que falten en vez de inventarlos (nombres, división, emails...).
- Troncales: para cambiar una o varias usá `update_trunks` (nunca `api_call` con el body completo: es enorme). Pasale
  solo lo que cambia (`suppress_username` o `properties`) y la selección (`trunks` y/o `name_pattern`); el plan muestra
  el antes/después por troncal. `list_trunks` y `trunk_properties` sirven para ver el estado y los nombres de propiedades.
- DIDs: para dar de alta varios números usá `create_did_pools` (E.164, un pool por número, salta los ya existentes), no
  `api_call` uno por uno.
- Wrap-up codes entre orgs (p.ej. réplica a una org de DRP): usá `copy_wrapup_codes` (org = origen, target_org = destino).
  Lee y escribe dentro del servidor: NO listes los códigos ni pases nombres por el chat. Crea hasta `max_create` por
  llamada; repetí hasta que `pendientes` sea 0. `create_wrapup_codes` es solo para unos pocos nombres que da el usuario.
- Scripts entre orgs: si el script llama data actions (`bridge.bridge` con `custom_-_<id>`) usá
  `clone_script_with_data_actions`, que copia los data actions, reescribe los ids y sube el script. `clone_script` solo
  copia el script y deja los ids del origen. Las credenciales de las integraciones nunca se copian.
- Script por defecto en colas: para asociar un script como default en varias colas/canales usá `set_queue_default_script`
  (por lista, patrón o `all_queues`), nunca `update_queue` cola por cola. Por defecto respeta un script distinto ya
  asignado; `replace_existing=true` lo reemplaza. Los canales salen de las features habilitadas del script.
- Empezá con `list_orgs` / `test_connection` si no sabés qué orgs hay.
- Para algo sin tool dedicada: `api_search` -> `api_describe` -> `api_call` (también pasa por la confirmación).
- Las orgs con read_only bloquean escrituras; allow_delete=false bloquea borrados. No intentes esquivarlo.
"""

mcp = MCPServer("genesys-cloud", instructions=INSTRUCTIONS, version=__version__, log_level="WARNING")

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

OrgArg = Annotated[
    str,
    Field(description="Cliente sobre el que actuar: nombre del perfil o alias (ver list_orgs). Obligatorio, no hay default."),
]

_settings: Settings | None = None
_clients: dict[str, GenesysClient] = {}


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def client(org: str | None) -> GenesysClient:
    profile = settings().get(org)
    if profile.name not in _clients:
        _clients[profile.name] = GenesysClient(profile, audit=AuditLog(settings().data_dir / "audit.jsonl"))
    return _clients[profile.name]


def reset_for_tests(new_settings: Settings | None, clients: dict[str, GenesysClient] | None = None) -> None:
    global _settings
    _settings = new_settings
    _clients.clear()
    _clients.update(clients or {})


# -- helpers --------------------------------------------------------------

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def is_uuid(value: str) -> bool:
    return bool(_UUID.match(value.strip()))


def slim(entity: dict | None, *fields: str) -> dict:
    """Keep only selected keys; nested `x.y` keys flatten into `x_y`."""
    if not entity:
        return {}
    out: dict[str, Any] = {}
    for f in fields:
        cur: Any = entity
        for part in f.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is not None:
            out[f.replace(".", "_")] = cur
    return out


def ref(entity: dict) -> dict:
    return {"id": entity.get("id"), "name": entity.get("name")}


class NotFound(GenesysError):
    pass


async def find_by_name(c: GenesysClient, path: str, name: str, kind: str, **extra: Any) -> dict:
    """Exact (case-insensitive) name lookup on a listing endpoint that supports ?name=."""
    items = await c.paginate(path, {"name": name, **extra}, max_items=500)
    matches = [i for i in items if (i.get("name") or "").lower() == name.lower()]
    if not matches:
        raise NotFound(f"No encontré {kind} con nombre {name!r} en la org {c.profile.name!r}.")
    if len(matches) > 1:
        ids = ", ".join(m["id"] for m in matches)
        raise GenesysError(f"Hay {len(matches)} {kind} llamados {name!r} ({ids}); pasá el id.")
    return matches[0]


async def resolve_id(c: GenesysClient, path: str, value: str, kind: str) -> str:
    if is_uuid(value):
        return value.strip()
    return (await find_by_name(c, path, value, kind))["id"]


async def resolve_user_id(c: GenesysClient, value: str) -> str:
    """Accept a user id or an email."""
    value = value.strip()
    if is_uuid(value):
        return value
    if "@" not in value:
        raise GenesysError(f"Usuario {value!r}: pasá el id o el email.")
    body = {"query": [{"fields": ["email"], "value": value, "type": "EXACT"}], "pageSize": 5}
    data = await c.request("POST", "/api/v2/users/search", json_body=body)
    results = (data or {}).get("results") or []
    if not results:
        raise NotFound(f"No encontré usuario con email {value!r} en la org {c.profile.name!r}.")
    return results[0]["id"]


async def home_division_id(c: GenesysClient) -> str:
    return (await c.get("/api/v2/authorization/divisions/home"))["id"]


async def resolve_division_id(c: GenesysClient, value: str | None) -> str:
    if not value:
        return await home_division_id(c)
    return await resolve_id(c, "/api/v2/authorization/divisions", value, "división")


def deep_merge(base: dict, changes: dict) -> dict:
    out = dict(base)
    for k, v in changes.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def chunks(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]

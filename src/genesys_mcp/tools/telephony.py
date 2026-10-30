"""Telephony: external/BYOC trunks (listed and changed in bulk) and DID pools (created in bulk).

Genesys only accepts a full PUT of a trunk (100+ properties), so `update_trunks` reads each trunk, flips the
requested `properties.<name>.value.instance` values and sends the rest back untouched. The caller only states
what to change; the plan shows a per-trunk before/after instead of the whole body.
"""

from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase
from typing import Annotated, Any, Literal

from pydantic import Field

from ..app import READ, WRITE, ConfirmArg, OrgArg, client, confirmable, is_uuid, mcp
from ..client import GenesysClient, GenesysError
from ..confirm import planning, warn

TRUNKS = "/api/v2/telephony/providers/edges/trunkbasesettings"
DID_POOLS = "/api/v2/telephony/providers/edges/didpools"
MAX_TRUNKS_PER_CALL = 50
MAX_DIDS_PER_CALL = 200
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")

SUPPRESS_DID = "trunk_outboundIdentity_suppress_username_if_did_available"
SUPPRESS_NO_DID = "trunk_outboundIdentity_suppress_username_if_no_did"

_READONLY_TRUNK_KEYS = (
    "dateCreated", "dateModified", "modifiedBy", "createdBy", "modifiedByApp", "createdByApp", "selfUri",
)
_REF_KEYS = ("trunkMetabase", "site", "inboundSite", "edgeGroup", "edge")

TrunkType = Literal["EXTERNAL", "EDGE", "PHONE"]
NamePatternArg = Annotated[
    str | None,
    Field(description="Patrón de nombre con comodines, sin distinguir mayúsculas. Ej: 'Telyaco outbound*' o '*57311045*'."),
]


def _effective(trunk: dict, key: str) -> Any:
    value = ((trunk.get("properties") or {}).get(key) or {}).get("value") or {}
    return value["instance"] if "instance" in value else value.get("default")


def _suppress_username(trunk: dict) -> str | None:
    did, no_did = _effective(trunk, SUPPRESS_DID), _effective(trunk, SUPPRESS_NO_DID)
    if did is None and no_did is None:
        return None
    if did and no_did:
        return "Always"
    if not did and not no_did:
        return "Never"
    return f"Parcial (con DID: {did}, sin DID: {no_did})"


async def _all_trunks(c: GenesysClient, trunk_type: str | None = None) -> list[dict]:
    trunks = await c.paginate(TRUNKS, {"expand": "properties"})  # the endpoint has no server-side type filter
    return [t for t in trunks if not trunk_type or t.get("trunkType") == trunk_type]


def _select(trunks: list[dict], org: str, names_or_ids: list[str], pattern: str | None) -> list[dict]:
    if not names_or_ids and not pattern:
        raise GenesysError("Indicá qué troncales cambiar: `trunks` (ids o nombres exactos) y/o `name_pattern`.")
    chosen: dict[str, dict] = {}
    for ref in names_or_ids:
        if is_uuid(ref):
            hits = [t for t in trunks if t["id"].lower() == ref.strip().lower()]
        else:
            hits = [t for t in trunks if (t.get("name") or "").lower() == ref.strip().lower()]
        if not hits:
            raise GenesysError(f"No encontré la troncal {ref!r} en la org {org!r}.")
        if len(hits) > 1:
            raise GenesysError(f"Hay {len(hits)} troncales llamadas {ref!r}; pasá el id.")
        chosen[hits[0]["id"]] = hits[0]
    if pattern:
        matches = [t for t in trunks if fnmatchcase((t.get("name") or "").lower(), pattern.lower())]
        if not matches and not chosen:
            raise GenesysError(f"Ninguna troncal coincide con {pattern!r} en la org {org!r}.")
        for t in matches:
            chosen[t["id"]] = t
    return sorted(chosen.values(), key=lambda t: t.get("name") or "")


def _check_value(key: str, schema: dict, value: Any) -> None:
    """Validate a new value against the property's own schema (type, enum, range)."""
    if schema.get("readonly"):
        raise GenesysError(f"La propiedad {key!r} es de solo lectura.")
    kind = schema.get("type")
    ok = {
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "string": isinstance(value, str),
        "array": isinstance(value, list),
    }.get(kind, True)
    if not ok:
        raise GenesysError(f"{key!r} espera un valor {kind} y recibió {value!r}.")
    if kind == "string" and schema.get("enum") and value not in schema["enum"]:
        raise GenesysError(f"{key!r}: {value!r} no es válido. Opciones: {schema['enum']}.")
    if kind == "integer":
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise GenesysError(f"{key!r}: {value} fuera del rango permitido [{lo}, {hi}].")
    if kind == "array":
        allowed = (schema.get("items") or {}).get("enum")
        bad = [v for v in value if allowed and v not in allowed]
        if bad:
            raise GenesysError(f"{key!r}: valores no válidos {bad}. Opciones: {allowed}.")
        if len(value) < schema.get("minItems", 0):
            raise GenesysError(f"{key!r} necesita al menos {schema['minItems']} elemento(s).")


def _put_body(trunk: dict, new_values: dict[str, Any]) -> dict:
    """The trunk as Genesys expects it back: no read-only fields, refs reduced to ids, properties as bare values."""
    body = {k: v for k, v in trunk.items() if k not in _READONLY_TRUNK_KEYS}
    for key in _REF_KEYS:
        if isinstance(body.get(key), dict):
            body[key] = {"id": body[key]["id"]}
    props = {name: {"value": dict(p.get("value") or {})} for name, p in (trunk.get("properties") or {}).items()}
    for key, new in new_values.items():
        props[key]["value"]["instance"] = new
    body["properties"] = props
    return body


@mcp.tool(annotations=READ)
async def list_trunks(
    org: OrgArg,
    name_pattern: NamePatternArg = None,
    trunk_type: Annotated[TrunkType | None, Field(description="Filtrar por tipo de troncal.")] = None,
    properties: Annotated[
        list[str], Field(description="Nombres de propiedades cuyo valor actual quieres ver en cada troncal.")
    ] = [],
) -> dict:
    """Lista troncales con su tipo, sites y el estado de 'Suppress user name' (Always/Never); opcionalmente otras propiedades."""
    c = client(org)
    trunks = await _all_trunks(c, trunk_type)
    if name_pattern:
        trunks = [t for t in trunks if fnmatchcase((t.get("name") or "").lower(), name_pattern.lower())]
    rows = []
    for t in sorted(trunks, key=lambda t: t.get("name") or ""):
        row: dict[str, Any] = {
            "id": t["id"],
            "name": t.get("name"),
            "type": t.get("trunkType"),
            "state": t.get("state"),
            "version": t.get("version"),
            "site": (t.get("site") or {}).get("name"),
            "inbound_site": (t.get("inboundSite") or {}).get("name"),
        }
        if (suppress := _suppress_username(t)) is not None:
            row["suppress_username"] = suppress
        for key in properties:
            row[key] = _effective(t, key)
        rows.append(row)
    return {"org": c.profile.name, "count": len(rows), "trunks": rows}


@mcp.tool(annotations=READ)
async def trunk_properties(
    trunk: Annotated[str, Field(description="Id o nombre exacto de la troncal.")],
    org: OrgArg,
    contains: Annotated[
        str | None, Field(description="Solo propiedades cuyo nombre contenga este texto, p.ej. 'suppress' o 'recording'.")
    ] = None,
) -> dict:
    """Muestra las propiedades de una troncal (nombre, tipo, valores permitidos, valor actual y por defecto) para saber qué pasar a update_trunks."""
    c = client(org)
    found = _select(await _all_trunks(c), c.profile.name, [trunk], None)[0]
    rows = []
    for name, p in sorted((found.get("properties") or {}).items()):
        if contains and contains.lower() not in name.lower():
            continue
        value = p.get("value") or {}
        row = {"name": name, "type": p.get("type"), "current": _effective(found, name), "default": value.get("default")}
        if p.get("enum") or (p.get("items") or {}).get("enum"):
            row["options"] = p.get("enum") or p["items"]["enum"]
        if p.get("readonly"):
            row["readonly"] = True
        for bound in ("minimum", "maximum"):
            if bound in p:
                row[bound] = p[bound]
        rows.append(row)
    return {"org": c.profile.name, "trunk": {"id": found["id"], "name": found.get("name")}, "properties": rows}


@mcp.tool(annotations=WRITE)
@confirmable
async def update_trunks(
    org: OrgArg,
    trunks: Annotated[list[str], Field(description="Troncales por id o nombre exacto.")] = [],
    name_pattern: NamePatternArg = None,
    suppress_username: Annotated[
        Literal["Always", "Never"] | None,
        Field(description="Atajo para 'Suppress user name' de la troncal: fija a la vez los dos flags de la API."),
    ] = None,
    properties: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Otras propiedades a fijar: {nombre_de_propiedad: valor}, con el tipo que corresponde "
                "(booleano, número, texto o lista). Ej: {'trunk_maxCallRate': '60/5s', 'trunk_recording_enabled': false}. "
                "Usá trunk_properties para ver los nombres y valores permitidos."
            )
        ),
    ] = {},
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Cambia propiedades de varias troncales a la vez (selección por ids/nombres y/o patrón con comodines).

    Lee cada troncal, cambia solo lo pedido y la guarda con el resto igual. Las que ya tienen el valor pedido no se tocan.
    El plan muestra el antes/después por troncal; no hace falta pasar la configuración completa.
    """
    desired = dict(properties)
    if suppress_username:
        clash = {SUPPRESS_DID, SUPPRESS_NO_DID} & set(desired)
        if clash:
            raise GenesysError(f"Pediste suppress_username y también {sorted(clash)} en `properties`: usá solo uno.")
        desired |= {SUPPRESS_DID: suppress_username == "Always", SUPPRESS_NO_DID: suppress_username == "Always"}
    if not desired:
        raise GenesysError("Indicá qué cambiar: `suppress_username` y/o `properties`.")

    c = client(org)
    selected = _select(await _all_trunks(c), c.profile.name, trunks, name_pattern)
    if len(selected) > MAX_TRUNKS_PER_CALL:
        raise GenesysError(
            f"La selección tiene {len(selected)} troncales (máximo {MAX_TRUNKS_PER_CALL} por llamada). Acotala con `name_pattern`."
        )
    unknown = [k for k in desired if not any(k in (t.get("properties") or {}) for t in selected)]
    if unknown:
        raise GenesysError(
            f"Ninguna de las troncales seleccionadas tiene la propiedad {unknown}. Usá trunk_properties para ver los nombres."
        )

    updated, unchanged, without = [], [], []
    for t in selected:
        props = t.get("properties") or {}
        missing = [k for k in desired if k not in props]
        if missing:
            without.append({"name": t.get("name"), "falta": missing})
            continue
        diff = {}
        for key, new in desired.items():
            _check_value(key, props[key], new)
            old = _effective(t, key)
            if old != new:
                diff[key] = {"antes": old, "despues": new}
        if not diff:
            unchanged.append(t.get("name"))
            continue
        saved = await c.request(
            "PUT",
            f"{TRUNKS}/{t['id']}",
            json_body=_put_body(t, {k: v["despues"] for k, v in diff.items()}),
            tool="update_trunks",
            plan_preview={"troncal": t.get("name"), "cambios": diff},
        )
        version = None if planning() else (saved or {}).get("version")
        updated.append({"id": t["id"], "name": t.get("name"), "version": version, "cambios": diff})

    if updated:
        warn(
            "Guardar una troncal puede reiniciar su registro/conexión y cortar llamadas en curso por ella: "
            "conviene hacerlo en un horario de poco tráfico."
        )
    out: dict[str, Any] = {"org": c.profile.name, "updated": updated, "sin_cambios": unchanged}
    if without:
        out["omitidas_sin_la_propiedad"] = without
    return out


def _covered_by(number: str, pools: list[dict]) -> dict | None:
    """The existing pool whose range already includes `number` (compared only against ranges of the same length)."""
    for pool in pools:
        start, end = pool.get("startPhoneNumber") or "", pool.get("endPhoneNumber") or ""
        if len(start) == len(end) == len(number) and start <= number <= end:
            return pool
    return None


@mcp.tool(annotations=READ)
async def list_did_pools(
    org: OrgArg,
    contains: Annotated[str | None, Field(description="Solo pools cuyo rango o comentario contenga este texto.")] = None,
) -> dict:
    """Lista los DID pools (rangos de números) de la org."""
    c = client(org)
    pools = await c.paginate(DID_POOLS)
    rows = [
        {"id": p["id"], "start": p.get("startPhoneNumber"), "end": p.get("endPhoneNumber"),
         "comments": p.get("comments"), "provider": p.get("provider"), "state": p.get("state")}
        for p in pools
    ]
    if contains:
        rows = [r for r in rows if contains.lower() in json.dumps(r, ensure_ascii=False).lower()]
    return {"org": c.profile.name, "count": len(rows), "pools": rows}


@mcp.tool(annotations=WRITE)
@confirmable
async def create_did_pools(
    numbers: Annotated[
        list[str],
        Field(description="Números en formato E.164 (con +, código de país y sin espacios), p.ej. ['+576054368361']. Cada uno crea un pool de un solo número.", min_length=1),
    ],
    org: OrgArg,
    comments: Annotated[str | None, Field(description="Comentario a poner en cada pool.")] = None,
    provider: Annotated[Literal["PURE_CLOUD", "PURE_CLOUD_VOICE"], Field(description="Provider de los pools.")] = "PURE_CLOUD",
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Crea DID pools de un solo número en bloque (idempotente: los números ya cubiertos por un pool existente no se tocan)."""
    c = client(org)
    bad = [n for n in numbers if not _E164.match(n)]
    if bad:
        raise GenesysError(f"Números fuera de formato E.164 (+código de país y dígitos, sin espacios): {bad[:10]}")
    unique = list(dict.fromkeys(numbers))
    if len(unique) > MAX_DIDS_PER_CALL:
        raise GenesysError(f"Son {len(unique)} números (máximo {MAX_DIDS_PER_CALL} por llamada); dividí la lista.")

    pools = await c.paginate(DID_POOLS)
    created, existing = [], []
    for number in unique:
        if pool := _covered_by(number, pools):
            existing.append({"number": number, "pool_id": pool["id"]})
            continue
        body: dict[str, Any] = {
            "name": number, "startPhoneNumber": number, "endPhoneNumber": number, "provider": provider,
        }
        if comments:
            body["comments"] = comments
        pool = await c.request("POST", DID_POOLS, json_body=body, tool="create_did_pools", plan_preview=body)
        created.append({"number": number, "pool_id": None if planning() else (pool or {}).get("id")})
    if len(unique) != len(numbers):
        warn(f"La lista traía {len(numbers) - len(unique)} número(s) repetido(s); se crea uno solo por número.")
    return {"org": c.profile.name, "created": created, "ya_existian": existing}

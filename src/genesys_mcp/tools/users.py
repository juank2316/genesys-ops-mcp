"""Users: search, read, create (with onboarding), update, roles, skills, languages, delete."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from ..app import (
    DESTRUCTIVE,
    READ,
    WRITE,
    ConfirmArg,
    NotFound,
    OrgArg,
    client,
    confirmable,
    is_uuid,
    mcp,
    ref,
    resolve_division_id,
    resolve_id,
    resolve_user_id,
    slim,
)
from ..client import GenesysAPIError, GenesysClient, GenesysError

UserArg = Annotated[str, Field(description="Id o email del usuario.")]

_USER_FIELDS = ("id", "name", "email", "username", "state", "department", "title", "division.name", "manager.id")

UserExpand = Literal[
    "skills", "languages", "groups", "authorization", "routingStatus", "presence", "station",
    "team", "locations", "profileSkills", "certifications", "employerInfo", "dateLastLogin",
]


class ProficiencyItem(BaseModel):
    name: str = Field(description="Nombre o id del skill / idioma.")
    proficiency: float = Field(default=3, ge=0, le=5, description="Nivel 0-5 (default 3).")


ProficiencyList = Annotated[list[ProficiencyItem], Field(description="Skills/idiomas con su nivel.")]
Mode = Annotated[
    Literal["add", "remove", "replace"],
    Field(description="add = agrega/actualiza; remove = quita; replace = deja exactamente esta lista."),
]


# -- read -----------------------------------------------------------------


@mcp.tool(annotations=READ)
async def search_users(
    org: OrgArg,
    query: Annotated[str | None, Field(description="Texto a buscar en nombre, email o username. Omitir = listar.")] = None,
    state: Annotated[Literal["active", "inactive", "any"], Field(description="Estado del usuario.")] = "active",
    department: Annotated[str | None, Field(description="Filtrar por departamento exacto.")] = None,
    max_results: Annotated[int, Field(ge=1, le=2000)] = 50,
) -> dict:
    """Busca usuarios por texto (nombre/email/username), estado y departamento."""
    c = client(org)
    criteria: list[dict[str, Any]] = []
    if query:
        criteria.append({"fields": ["name", "email", "username"], "value": query, "type": "CONTAINS"})
    if department:
        criteria.append({"fields": ["department"], "value": department, "type": "EXACT"})
    if state != "any":
        criteria.append({"fields": ["state"], "value": state, "type": "EXACT"})

    if not criteria:
        users = await c.paginate("/api/v2/users", {"state": "any"}, max_items=max_results)
        total = len(users)
    else:
        users, page, total = [], 1, 0
        while len(users) < max_results:
            body = {"query": criteria, "pageSize": min(100, max_results), "pageNumber": page}
            data = await c.request("POST", "/api/v2/users/search", json_body=body) or {}
            users.extend(data.get("results") or [])
            total = data.get("total", len(users))
            if page >= data.get("pageCount", 1):
                break
            page += 1
        users = users[:max_results]
    return {"org": c.profile.name, "total": total, "returned": len(users), "users": [slim(u, *_USER_FIELDS) for u in users]}


@mcp.tool(annotations=READ)
async def get_user(
    user: UserArg,
    org: OrgArg,
    include: Annotated[list[UserExpand], Field(description="Datos extra a incluir.")] = ["skills", "languages", "authorization"],
    include_queues: Annotated[bool, Field(description="Incluir colas a las que pertenece.")] = True,
) -> dict:
    """Detalle de un usuario: datos básicos y opcionalmente skills, idiomas, roles, grupos, colas, etc."""
    c = client(org)
    user_id = await resolve_user_id(c, user)
    params: dict[str, Any] = {"expand": ",".join(include)} if include else {}
    data = await c.request("GET", f"/api/v2/users/{user_id}", params=params)
    out: dict[str, Any] = {"org": c.profile.name, "user": data}
    if include_queues:
        queues = await c.paginate(f"/api/v2/users/{user_id}/queues")
        out["queues"] = [{**ref(q), "joined": q.get("joined")} for q in queues]
    return out


# -- create / update ------------------------------------------------------


async def _role_ids(c: GenesysClient, roles: list[str]) -> list[str]:
    return [await resolve_id(c, "/api/v2/authorization/roles", r, "rol") for r in roles]


async def _proficiencies(c: GenesysClient, path: str, kind: str, items: list[ProficiencyItem]) -> list[dict]:
    return [{"id": await resolve_id(c, path, i.name, kind), "proficiency": i.proficiency} for i in items]


@mcp.tool(annotations=WRITE)
@confirmable
async def create_user(
    name: str,
    email: str,
    org: OrgArg,
    division: Annotated[str | None, Field(description="División (id o nombre). Omitir = Home.")] = None,
    department: str | None = None,
    title: str | None = None,
    password: Annotated[str | None, Field(description="Contraseña inicial (opcional; si no, se envía invitación).")] = None,
    state: Annotated[Literal["active", "inactive"] | None, Field(description="Estado inicial.")] = None,
    roles: Annotated[list[str], Field(description="Roles a asignar (nombres o ids).")] = [],
    role_division: Annotated[str | None, Field(description="División donde aplican los roles. Omitir = Home.")] = None,
    skills: Annotated[list[ProficiencyItem], Field(description="Skills con nivel.")] = [],
    languages: Annotated[list[ProficiencyItem], Field(description="Idiomas con nivel.")] = [],
    queues: Annotated[list[str], Field(description="Colas a las que agregarlo (nombres o ids).")] = [],
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Alta de usuario completa (onboarding): crea el usuario y opcionalmente asigna roles, skills, idiomas y colas.

    Si ya existe un usuario con ese email no lo duplica: reutiliza el existente y aplica las asignaciones,
    así que es seguro reintentar después de un fallo parcial. Devuelve el resultado de cada paso.
    """
    c = client(org)
    steps: dict[str, Any] = {}
    try:
        user_id = await resolve_user_id(c, email)
        steps["user"] = {"status": "already_existed", "id": user_id}
    except NotFound:
        body: dict[str, Any] = {"name": name, "email": email, "divisionId": await resolve_division_id(c, division)}
        for key, value in (("department", department), ("title", title), ("password", password), ("state", state)):
            if value:
                body[key] = value
        created = await c.request("POST", "/api/v2/users", json_body=body, tool="create_user")
        user_id = created["id"]
        steps["user"] = {"status": "created", "id": user_id}

    async def step(label: str, coro) -> None:
        try:
            steps[label] = {"status": "ok", "result": await coro}
        except GenesysError as exc:
            steps[label] = {"status": "error", "error": str(exc)}

    if roles:
        await step("roles", _apply_roles(c, user_id, roles, role_division, "add"))
    if skills:
        await step("skills", _apply_proficiencies(c, user_id, "skill", skills, "add"))
    if languages:
        await step("languages", _apply_proficiencies(c, user_id, "language", languages, "add"))
    if queues:
        from .queues import _change_members

        async def add_to_queues():
            return [await _change_members(c, q, [user_id], remove=False) for q in queues]

        await step("queues", add_to_queues())
    return {"org": c.profile.name, "user_id": user_id, "steps": steps}


_UPDATABLE = {"name", "department", "title", "email", "preferredName", "manager", "state", "acdAutoAnswer", "addresses", "username"}


@mcp.tool(annotations=WRITE)
@confirmable
async def update_user(
    user: UserArg,
    org: OrgArg,
    changes: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Campos a cambiar: name, department, title, email, username, preferredName, state "
                "('active'|'inactive'), acdAutoAnswer (bool), addresses, manager (id o email del manager)."
            )
        ),
    ],
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Modifica datos de un usuario (maneja automáticamente el campo version)."""
    c = client(org)
    unknown = set(changes) - _UPDATABLE
    if unknown:
        raise GenesysError(f"Campos no soportados: {sorted(unknown)}. Permitidos: {sorted(_UPDATABLE)}.")
    user_id = await resolve_user_id(c, user)
    body = dict(changes)
    if manager := body.get("manager"):
        body["manager"] = await resolve_user_id(c, manager)
    for attempt in range(2):
        current = await c.get(f"/api/v2/users/{user_id}")
        body["version"] = current["version"]
        try:
            updated = await c.request("PATCH", f"/api/v2/users/{user_id}", json_body=body, tool="update_user")
            return {"org": c.profile.name, "user": slim(updated, *_USER_FIELDS)}
        except GenesysAPIError as exc:
            if exc.status != 409 or attempt:
                raise
    raise AssertionError("unreachable")


# -- roles ----------------------------------------------------------------


async def _apply_roles(c: GenesysClient, user_id: str, roles: list[str], division: str | None, mode: str) -> dict:
    division_id = await resolve_division_id(c, division)
    grants = [{"roleId": rid, "divisionId": division_id} for rid in await _role_ids(c, roles)]
    endpoint = {"add": "bulkadd", "remove": "bulkremove", "replace": "bulkreplace"}[mode]
    params = {"subjectType": "PC_USER"} if mode != "remove" else None
    await c.request(
        "POST",
        f"/api/v2/authorization/subjects/{user_id}/{endpoint}",
        params=params,
        json_body={"grants": grants},
        tool=f"set_user_roles:{mode}",
    )
    return {"mode": mode, "division_id": division_id, "role_ids": [g["roleId"] for g in grants]}


@mcp.tool(annotations=WRITE)
@confirmable
async def set_user_roles(
    user: UserArg,
    roles: Annotated[list[str], Field(description="Roles (nombres o ids).", min_length=1)],
    org: OrgArg,
    mode: Mode = "add",
    division: Annotated[str | None, Field(description="División donde aplican. Omitir = Home.")] = None,
    confirmation_token: ConfirmArg = None,
) -> dict:
    """Asigna, quita o reemplaza roles de un usuario en una división.

    Ojo: mode='replace' deja SOLO estos roles (en todas las divisiones), incluido quitar 'employee' si no está en la lista.
    """
    c = client(org)
    user_id = await resolve_user_id(c, user)
    result = await _apply_roles(c, user_id, roles, division, mode)
    subject = await c.get(f"/api/v2/authorization/subjects/{user_id}")
    current = [
        {"role": g.get("role", {}).get("name"), "division": g.get("division", {}).get("name")}
        for g in subject.get("grants", [])
    ]
    return {"org": c.profile.name, "user_id": user_id, **result, "current_grants": current}


# -- skills / languages ---------------------------------------------------

_PROF = {
    "skill": ("/api/v2/routing/skills", "routingskills", "skill"),
    "language": ("/api/v2/routing/languages", "routinglanguages", "idioma"),
}


async def _apply_proficiencies(c: GenesysClient, user_id: str, kind: str, items: list[ProficiencyItem], mode: str) -> dict:
    lookup_path, sub, label = _PROF[kind]
    base = f"/api/v2/users/{user_id}/{sub}"
    tool = f"set_user_{kind}s:{mode}"
    if mode == "remove":
        ids = [await resolve_id(c, lookup_path, i.name, label) for i in items]
        for item_id in ids:
            await c.request("DELETE", f"{base}/{item_id}", tool=tool)
        return {"mode": mode, "removed": ids}

    body = await _proficiencies(c, lookup_path, label, items)
    if mode == "replace":
        if kind == "skill":
            await c.request("PUT", f"{base}/bulk", json_body=body, tool=tool)
        else:
            wanted = {b["id"] for b in body}
            for cur in await c.paginate(base):
                if cur["id"] not in wanted:
                    await c.request("DELETE", f"{base}/{cur['id']}", tool=tool)
            await c.request("PATCH", f"{base}/bulk", json_body=body, tool=tool)
    else:
        await c.request("PATCH", f"{base}/bulk", json_body=body, tool=tool)
    current = await c.paginate(base)
    return {"mode": mode, "current": [{"name": x.get("name"), "proficiency": x.get("proficiency")} for x in current]}


@mcp.tool(annotations=WRITE)
@confirmable
async def set_user_skills(
    user: UserArg, skills: ProficiencyList, org: OrgArg, mode: Mode = "add", confirmation_token: ConfirmArg = None
) -> dict:
    """Agrega/actualiza, quita o reemplaza los ACD skills de un usuario (con nivel 0-5)."""
    c = client(org)
    user_id = await resolve_user_id(c, user)
    return {"org": c.profile.name, "user_id": user_id, **await _apply_proficiencies(c, user_id, "skill", skills, mode)}


@mcp.tool(annotations=WRITE)
@confirmable
async def set_user_languages(
    user: UserArg, languages: ProficiencyList, org: OrgArg, mode: Mode = "add", confirmation_token: ConfirmArg = None
) -> dict:
    """Agrega/actualiza, quita o reemplaza los idiomas de routing de un usuario (con nivel 0-5)."""
    c = client(org)
    user_id = await resolve_user_id(c, user)
    return {"org": c.profile.name, "user_id": user_id, **await _apply_proficiencies(c, user_id, "language", languages, mode)}


# -- delete ---------------------------------------------------------------


@mcp.tool(annotations=DESTRUCTIVE)
@confirmable
async def delete_user(user: UserArg, org: OrgArg, confirmation_token: ConfirmArg = None) -> dict:
    """Elimina un usuario (pasa a estado 'deleted'). Requiere allow_delete en el perfil. Considerá update_user state='inactive' como alternativa."""
    c = client(org)
    user_id = await resolve_user_id(c, user)
    if not is_uuid(user_id):
        raise GenesysError("Id de usuario inválido.")
    data = await c.get(f"/api/v2/users/{user_id}")
    await c.request("DELETE", f"/api/v2/users/{user_id}", tool="delete_user")
    return {"org": c.profile.name, "deleted": slim(data, "id", "name", "email")}

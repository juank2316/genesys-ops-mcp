from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..app import READ, OrgArg, client, mcp, ref, settings, slim


@mcp.tool(annotations=READ)
async def list_orgs() -> dict:
    """Lista los clientes (perfiles de org) configurados, sin secretos: nombre, alias, ambiente, región y modo de seguridad."""
    s = settings()
    return {
        "config_source": s.source,
        "orgs": [p.public_view() for p in s.orgs.values()],
    }


@mcp.tool(annotations=READ)
async def test_connection(org: OrgArg) -> dict:
    """Verifica credenciales y conectividad con una org: devuelve nombre/id de la org, división home y el modo del perfil."""
    c = client(org)
    me = await c.get("/api/v2/organizations/me")
    home = await c.get("/api/v2/authorization/divisions/home")
    return {
        **c.profile.public_view(),
        "organization": slim(me, "id", "name", "thirdPartyOrgName", "domain", "state", "defaultLanguage"),
        "home_division": ref(home),
    }


@mcp.tool(annotations=READ)
async def list_divisions(
    org: OrgArg,
    name: Annotated[str | None, Field(description="Filtro por nombre (soporta * como comodín).")] = None,
) -> dict:
    """Lista las divisiones de la org."""
    c = client(org)
    items = await c.paginate("/api/v2/authorization/divisions", {"name": name})
    return {"org": c.profile.name, "count": len(items), "divisions": [slim(d, "id", "name", "homeDivision") for d in items]}


@mcp.tool(annotations=READ)
async def list_roles(
    org: OrgArg,
    name: Annotated[str | None, Field(description="Filtro por nombre del rol.")] = None,
) -> dict:
    """Lista los roles de la org (id, nombre, cantidad de usuarios)."""
    c = client(org)
    items = await c.paginate("/api/v2/authorization/roles", {"name": name, "userCount": True})
    return {
        "org": c.profile.name,
        "count": len(items),
        "roles": [slim(r, "id", "name", "description", "userCount", "default") for r in items],
    }

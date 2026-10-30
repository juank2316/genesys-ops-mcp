"""Org profiles.

Lookup order for the profiles file (first match wins):
  1. $GENESYS_MCP_CONFIG
  2. ./.genesys_mcp/orgs.toml        (local override, gitignored)
  3. ~/.genesys_mcp/orgs.toml        (per-user override)
  4. orgs.toml shipped in this package (the team's shared client list; it holds no secrets)
If none exists, a single "default" profile is built from GENESYS_CLIENT_ID /
GENESYS_CLIENT_SECRET / GENESYS_REGION env vars.

Audit log and caches go next to an override file, or to ~/.genesys_mcp when the
packaged list is used (never inside the installed package).

There is no default org on purpose: every tool call must name its org (by profile
name or alias), so a change can never land on the wrong client by omission.

Example orgs.toml:

    [orgs.lab]
    display_name = "Laboratorio InfoSys"
    environment = "lab"
    aliases = ["laboratorio"]
    region = "us-east-1"
    client_id = "..."
    client_secret = "..."          # or client_secret_env = "LAB_SECRET"
    read_only = false
    allow_delete = true

    [orgs.acme-prod]
    display_name = "ACME Colombia"
    environment = "prod"
    aliases = ["acme"]
    region = "sa-east-1"
    client_id = "..."
    client_secret_env = "ACME_PROD_SECRET"
    read_only = true
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .regions import resolve_domain

CONFIG_DIR_NAME = ".genesys_mcp"
CONFIG_FILE_NAME = "orgs.toml"
PACKAGED_CONFIG = Path(__file__).with_name(CONFIG_FILE_NAME)
USER_DATA_DIR = Path.home() / CONFIG_DIR_NAME
ENVIRONMENTS = ("lab", "dev", "qa", "prod")


class ConfigError(ToolError):
    """ToolError so the model sees the setup instructions, not a generic failure."""


@dataclass(frozen=True)
class OrgProfile:
    name: str
    region: str
    client_id: str
    client_secret: str
    read_only: bool = False
    allow_delete: bool = False
    rate_per_sec: float = 4.0
    max_concurrency: int = 4
    description: str = ""
    display_name: str = ""
    environment: str = "prod"
    aliases: tuple[str, ...] = ()

    @property
    def domain(self) -> str:
        return resolve_domain(self.region)

    @property
    def api_url(self) -> str:
        return f"https://api.{self.domain}"

    @property
    def apps_url(self) -> str:
        """Host of the script upload service (/uploads/v2/scripter): it does not exist on api.<domain>."""
        return f"https://apps.{self.domain}"

    @property
    def login_url(self) -> str:
        return f"https://login.{self.domain}"

    @property
    def keys(self) -> set[str]:
        """Lowercased name and aliases: what a user may type to refer to this org."""
        return {self.name.lower(), *(a.lower() for a in self.aliases)}

    @property
    def label(self) -> str:
        """Human tag shown on every plan/result, e.g. 'ACME Colombia (acme-prod) · PROD · sa-east-1'."""
        return f"{self.display_name or self.name} ({self.name}) · {self.environment.upper()} · {self.region}"

    def public_view(self) -> dict:
        """Profile without secrets, safe to show to the model."""
        return {
            "org": self.name,
            "display_name": self.display_name or self.name,
            "environment": self.environment,
            "aliases": list(self.aliases),
            "region": self.region,
            "api_url": self.api_url,
            "read_only": self.read_only,
            "allow_delete": self.allow_delete,
            "description": self.description,
        }


@dataclass(frozen=True)
class Settings:
    orgs: dict[str, OrgProfile]
    source: str
    data_dir: Path

    def get(self, org: str | None) -> OrgProfile:
        """Resolve a profile by name or alias (case-insensitive). There is no default: org is mandatory."""
        available = ", ".join(sorted(self.orgs))
        if not org or not org.strip():
            raise ConfigError(
                "Falta indicar el cliente (org). Toda operación debe nombrar la org explícitamente; "
                f"preguntale al usuario sobre qué cliente actuar. Orgs disponibles: {available}."
            )
        key = org.strip().lower()
        for profile in self.orgs.values():
            if key in profile.keys:
                return profile
        raise ConfigError(f"Org {org!r} no está configurada (ni como nombre ni como alias). Orgs disponibles: {available}.")


def _candidate_files() -> list[Path]:
    paths = []
    if env := os.environ.get("GENESYS_MCP_CONFIG"):
        paths.append(Path(env).expanduser())
    paths.append(Path.cwd() / CONFIG_DIR_NAME / CONFIG_FILE_NAME)
    paths.append(USER_DATA_DIR / CONFIG_FILE_NAME)
    paths.append(PACKAGED_CONFIG)
    return paths


def config_path() -> Path | None:
    """The orgs.toml load_settings() would use, or None."""
    return next((p for p in _candidate_files() if p.is_file()), None)


def _profile_from_table(name: str, table: dict) -> OrgProfile:
    secret = table.get("client_secret", "")
    if not secret and (env_name := table.get("client_secret_env")):
        secret = os.environ.get(env_name, "")
        if not secret:
            raise ConfigError(f"Org {name!r}: la variable de entorno {env_name} (client_secret_env) está vacía.")
    client_id = table.get("client_id", "")
    if not client_id and (env_name := table.get("client_id_env")):
        client_id = os.environ.get(env_name, "")
    environment = str(table.get("environment", "prod")).lower()
    if environment not in ENVIRONMENTS:
        raise ConfigError(f"Org {name!r}: environment = {environment!r} no es válido. Usá uno de: {', '.join(ENVIRONMENTS)}.")
    aliases = table.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(a, str) and a.strip() for a in aliases):
        raise ConfigError(f"Org {name!r}: aliases debe ser una lista de textos, p.ej. aliases = [\"acme\"].")
    if not (client_id and secret):
        raise ConfigError(f"Org {name!r}: faltan client_id / client_secret.")
    if "region" not in table:
        raise ConfigError(f"Org {name!r}: falta 'region'.")
    profile = OrgProfile(
        name=name,
        region=str(table["region"]),
        client_id=client_id,
        client_secret=secret,
        read_only=bool(table.get("read_only", False)),
        allow_delete=bool(table.get("allow_delete", False)),
        rate_per_sec=float(table.get("rate_per_sec", 4.0)),
        max_concurrency=int(table.get("max_concurrency", 4)),
        description=str(table.get("description", "")),
        display_name=str(table.get("display_name", "")),
        environment=environment,
        aliases=tuple(a.strip() for a in aliases),
    )
    try:
        profile.domain  # validate region early
    except ValueError as exc:
        raise ConfigError(f"Org {name!r}: {exc}") from exc
    return profile


def _check_unique_names(orgs: dict[str, OrgProfile], path: Path) -> None:
    """A name or alias must point to exactly one org, or 'acme' could silently mean two clients."""
    owner: dict[str, str] = {}
    for profile in orgs.values():
        for key in profile.keys:
            if key in owner and owner[key] != profile.name:
                raise ConfigError(f"{path}: {key!r} se usa en las orgs {owner[key]!r} y {profile.name!r}; debe ser único.")
            owner[key] = profile.name


def load_settings() -> Settings:
    for path in _candidate_files():
        if path.is_file():
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{path}: TOML inválido: {exc}") from exc
            tables = data.get("orgs") or {}
            if not tables:
                raise ConfigError(f"{path}: no hay ninguna sección [orgs.<nombre>].")
            if "default" in data:
                raise ConfigError(
                    f"{path}: 'default' ya no se admite. Cada operación debe indicar la org explícitamente; "
                    "borrá la línea default = ..."
                )
            orgs = {name: _profile_from_table(name, t) for name, t in tables.items()}
            _check_unique_names(orgs, path)
            default_dir = USER_DATA_DIR if path == PACKAGED_CONFIG else path.parent
            data_dir = Path(os.environ.get("GENESYS_MCP_HOME", default_dir)).expanduser()
            return Settings(orgs=orgs, source=str(path), data_dir=data_dir)

    client_id = os.environ.get("GENESYS_CLIENT_ID", "")
    secret = os.environ.get("GENESYS_CLIENT_SECRET", "")
    if client_id and secret:
        profile = _profile_from_table(
            "default",
            {
                "region": os.environ.get("GENESYS_REGION", "us-east-1"),
                "client_id": client_id,
                "client_secret": secret,
                "read_only": os.environ.get("GENESYS_READ_ONLY", "false").lower() == "true",
                "allow_delete": os.environ.get("GENESYS_ALLOW_DELETE", "false").lower() == "true",
            },
        )
        data_dir = Path(os.environ.get("GENESYS_MCP_HOME", Path.cwd() / CONFIG_DIR_NAME)).expanduser()
        return Settings(orgs={"default": profile}, source="env", data_dir=data_dir)

    searched = "\n  ".join(str(p) for p in _candidate_files())
    raise ConfigError(
        "No hay orgs configuradas. Creá un orgs.toml (ver orgs.example.toml) en alguna de estas rutas:\n  "
        f"{searched}\no definí GENESYS_CLIENT_ID, GENESYS_CLIENT_SECRET y GENESYS_REGION."
    )

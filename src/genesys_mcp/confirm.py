"""Two-step confirmation for every tool that changes data.

First call (no `confirmation_token`): the tool runs in *plan mode*. Reads go to Genesys
as usual, so names are resolved and missing data surfaces as errors, but every write is
recorded instead of sent and answered with a synthetic response. The caller gets the plan
(org, exact requests, problems, warnings) and, only if nothing blocks it, a single-use token.

Second call (same arguments + token): the token is checked against a fingerprint of the
tool name and arguments, consumed, and the tool runs for real.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated, Any, Awaitable, Callable

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

TOKEN_TTL_SECONDS = 600
PLACEHOLDER_ID = "00000000-0000-0000-0000-000000000000"
_BODY_PREVIEW_CHARS = 1500
_SECRET_KEYS = {"password", "client_secret", "secret", "token"}

ConfirmArg = Annotated[
    str | None,
    Field(
        description=(
            "Omitir en la primera llamada: se devuelve el plan sin cambiar nada. Después de que el usuario lo apruebe, "
            "repetir la MISMA llamada con el token recibido para ejecutarla."
        )
    ),
]


class ConfirmationError(ToolError):
    pass


@dataclass
class Plan:
    writes: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def record(self, org: str, method: str, path: str, params: dict | None, body: Any, preview: Any = None) -> None:
        """`preview` replaces the body in the plan when the real one is too big to show what actually changes."""
        entry: dict[str, Any] = {"org": org, "method": method, "path": _show(path)}
        if params:
            entry["query"] = params
        shown = preview if preview is not None else body
        if shown is not None:
            text = json.dumps(_mask(shown), ensure_ascii=False, default=str)
            entry["body"] = _show(text if len(text) <= _BODY_PREVIEW_CHARS else text[:_BODY_PREVIEW_CHARS] + "…")
        self.writes.append(entry)


_plan: ContextVar[Plan | None] = ContextVar("genesys_plan", default=None)
_confirmation: ContextVar[str | None] = ContextVar("genesys_confirmation", default=None)
_tokens: dict[str, tuple[str, float]] = {}


def current_plan() -> Plan | None:
    return _plan.get()


def planning() -> bool:
    return _plan.get() is not None


def current_confirmation() -> str | None:
    return _confirmation.get()


def warn(message: str) -> None:
    """Attach a non-blocking warning to the plan being built (no-op when executing)."""
    if plan := _plan.get():
        plan.warnings.append(message)


def synthetic_write_response(path: str, body: Any) -> Any:
    """Stand-in for a write's response so the rest of the tool can keep planning."""
    base = {"id": PLACEHOLDER_ID, "correlationId": PLACEHOLDER_ID, "version": 1}
    return {**body, **base} if isinstance(body, dict) else base


def synthetic_read_response() -> dict:
    """Answer for reads of objects that only exist in the plan (their id is the placeholder)."""
    return {"id": PLACEHOLDER_ID, "name": "(nuevo)", "entities": [], "pageCount": 1, "grants": [], "succeeded": True}


def _show(text: str) -> str:
    return text.replace(PLACEHOLDER_ID, "<id-nuevo>")


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in _SECRET_KEYS else _mask(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _fingerprint(tool: str, args: dict) -> str:
    payload = json.dumps({"tool": tool, "args": _jsonable(args)}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _issue(fingerprint: str) -> str:
    now = time.monotonic()
    for tok in [t for t, (_, exp) in _tokens.items() if exp < now]:
        del _tokens[tok]
    token = secrets.token_urlsafe(12)
    _tokens[token] = (fingerprint, now + TOKEN_TTL_SECONDS)
    return token


def _consume(token: str, fingerprint: str) -> None:
    entry = _tokens.get(token)
    if entry is None:
        raise ConfirmationError(
            "confirmation_token inválido, ya usado o vencido. Volvé a llamar sin token para generar un plan nuevo "
            "y pedile confirmación al usuario otra vez."
        )
    expected, expires = entry
    if time.monotonic() > expires:
        del _tokens[token]
        raise ConfirmationError("El confirmation_token venció. Generá un plan nuevo y pedí confirmación otra vez.")
    if expected != fingerprint:
        raise ConfirmationError(
            "Los parámetros no coinciden con el plan que el usuario confirmó (cambió la org o algún argumento). "
            "Generá un plan nuevo sin token y pedí confirmación otra vez."
        )
    del _tokens[token]


def _problems(value: Any) -> list[str]:
    """Collect per-step errors that tools report inside their result instead of raising."""
    found: list[str] = []
    if isinstance(value, dict):
        if isinstance(value.get("error"), str):
            found.append(value["error"])
        for v in value.values():
            found.extend(_problems(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(_problems(v))
    return found


def confirmable(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Gate a write tool behind plan + confirmation. The tool must take `org` (and may take `target_org`)."""
    from .app import settings

    sig = inspect.signature(fn)
    tool = fn.__name__

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        call = dict(bound.arguments)
        token = call.pop("confirmation_token", None)
        profiles = []
        for key in ("org", "target_org"):
            if key == "org" or call.get(key):
                profile = settings().get(call.get(key))  # raises when the org is missing or unknown
                call[key] = profile.name  # alias and name must fingerprint the same
                profiles.append(profile)
        fingerprint = _fingerprint(tool, call)

        if token:
            _consume(token, fingerprint)
            reset = _confirmation.set(token)
            try:
                result = await fn(**bound.arguments)
            finally:
                _confirmation.reset(reset)
            if isinstance(result, dict):
                result = {"ejecutado_en": [p.label for p in profiles], **result}
            return result

        plan = Plan()
        reset = _plan.set(plan)
        try:
            preview, problems = await fn(**bound.arguments), []
        except ToolError as exc:
            preview, problems = None, [str(exc)]
        finally:
            _plan.reset(reset)
        problems += _problems(preview)

        header = {
            "accion": tool,
            "org": [p.label for p in profiles],
            "parametros": _mask(_jsonable(call)),
        }
        if problems:
            return {
                "status": "bloqueado",
                **header,
                "problemas": problems,
                "cambios_planeados": plan.writes,
                "instrucciones": (
                    "No se generó token: faltan datos o algo no es válido. Explicale los problemas al usuario, "
                    "pedile lo que falta y volvé a planificar. No ejecutes nada."
                ),
            }
        if not plan.writes:
            return {"status": "sin_cambios", **header, "resultado": preview}

        warnings = list(plan.warnings)
        if any(p.environment == "prod" for p in profiles):
            warnings.insert(0, "ORG PRODUCTIVA: el cambio afecta a un cliente en producción.")
        deletes = sum(1 for w in plan.writes if w["method"] == "DELETE")
        if deletes:
            warnings.append(f"Incluye {deletes} borrado(s) irreversible(s).")
        return {
            "status": "requiere_confirmacion",
            **header,
            "cambios_planeados": plan.writes,
            "advertencias": warnings,
            "vista_previa": preview,
            "confirmation_token": _issue(fingerprint),
            "expira_en_segundos": TOKEN_TTL_SECONDS,
            "instrucciones": (
                "Todavía NO se cambió nada. Mostrale al usuario la org, la acción y los cambios planeados, y "
                "preguntale si confirma. Solo si responde que sí, repetí exactamente la misma llamada agregando "
                "confirmation_token. Nunca confirmes por tu cuenta."
            ),
        }

    return wrapper

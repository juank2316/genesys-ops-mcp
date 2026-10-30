"""Async HTTP client for one Genesys Cloud org.

Retry, Retry-After and read-vs-mutation handling adapted from Genesys' ava-mcp
(MIT, see NOTICE): reads retry 429/5xx/transport errors; mutations retry only
429, because a 5xx or dropped connection leaves the outcome ambiguous and a
blind retry could create duplicates.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import random
import socket
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .config import OrgProfile
from .confirm import (
    PLACEHOLDER_ID,
    current_confirmation,
    current_plan,
    synthetic_read_response,
    synthetic_write_response,
)

logger = logging.getLogger(__name__)


def _local_actor() -> str:
    """Who is running this server ('user@host'), so the audit log tells team members apart."""
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:  # getuser() raises when no user env var / pwd entry exists
        return "desconocido"


_MAX_ATTEMPTS = 4
_BACKOFF_CAP = 8.0
_BACKOFF_TOTAL_CAP = 45.0
_TOKEN_LEEWAY = 120

# POSTs that only read data (searches, analytics queries, exports); allowed on read_only orgs.
_READ_POST_SUFFIXES = ("/search", "/query", "/export")


class GenesysError(ToolError):
    """Error surfaced to the model; message is meant to be actionable.

    Subclassing ToolError is what makes the MCP SDK pass the message to the model
    instead of a generic "Error executing tool".
    """


class GenesysAPIError(GenesysError):
    def __init__(self, status: int, method: str, path: str, body: Any, correlation_id: str | None):
        self.status = status
        self.body = body
        message = body.get("message") if isinstance(body, dict) else str(body)[:500]
        code = body.get("code") if isinstance(body, dict) else None
        hint = {
            401: "Credenciales inválidas o token revocado.",
            403: "El OAuth client no tiene el permiso/rol necesario (revisá el rol y su división).",
            404: "No existe el recurso (o no es visible en las divisiones del client).",
            409: "Conflicto: probablemente ya existe o la versión cambió; volvé a leer y reintentá.",
        }.get(status, "")
        super().__init__(
            f"Genesys {status} en {method} {path}: {message}"
            + (f" [code={code}]" if code else "")
            + (f" [correlationId={correlation_id}]" if correlation_id else "")
            + (f" -- {hint}" if hint else "")
        )


class PolicyError(GenesysError):
    """Blocked locally by the org profile (read_only / allow_delete)."""


def is_write(method: str, path: str) -> bool:
    method = method.upper()
    if method in ("GET", "HEAD", "OPTIONS"):
        return False
    if method == "POST" and path.split("?")[0].rstrip("/").endswith(_READ_POST_SUFFIXES):
        return False
    return True


class RateLimiter:
    """Token bucket + concurrency cap. Genesys limits client-credentials tokens to ~300 req/min."""

    def __init__(self, rate_per_sec: float, max_concurrency: int):
        self._rate = rate_per_sec
        self._capacity = max(1.0, rate_per_sec)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._sem = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> RateLimiter:
        await self._sem.acquire()
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    break
                await asyncio.sleep((1 - self._tokens) / self._rate)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._sem.release()


class AuditLog:
    """Append-only JSONL log of every mutation (who/what/where/result). Never logs secrets or bodies."""

    def __init__(self, path: Path | None):
        self.path = path

    def write(self, **entry: Any) -> None:
        if self.path is None:
            return
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("No se pudo escribir el audit log: %s", exc)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After", "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _backoff(attempt: int) -> float:
    return random.uniform(0.0, min(_BACKOFF_CAP, 2.0**attempt))


def _parse_body(resp: httpx.Response) -> Any:
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


class GenesysClient:
    def __init__(
        self,
        profile: OrgProfile,
        *,
        audit: AuditLog | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.profile = profile
        self.audit = audit or AuditLog(None)
        self._transport = transport
        self._http = httpx.AsyncClient(
            base_url=profile.api_url,
            timeout=httpx.Timeout(60.0, connect=15.0),
            transport=transport,
            headers={"User-Agent": f"genesys-ops-mcp/{__version__}"},
        )
        self._limiter = RateLimiter(profile.rate_per_sec, profile.max_concurrency)
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()
        self.actor = _local_actor()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- auth -------------------------------------------------------------

    async def _get_token(self, force: bool = False) -> str:
        async with self._token_lock:
            if not force and self._token and time.monotonic() < self._token_expires:
                return self._token
            resp = await self._http.post(
                f"{self.profile.login_url}/oauth/token",
                data={"grant_type": "client_credentials"},
                auth=(self.profile.client_id, self.profile.client_secret),
            )
            if resp.status_code >= 400:
                raise GenesysError(
                    f"Login falló para org {self.profile.name!r} ({resp.status_code}): {resp.text[:300]}. "
                    "Revisá client_id/client_secret, que el client sea de tipo Client Credentials y la región."
                )
            payload = resp.json()
            self._token = payload["access_token"]
            ttl = int(payload.get("expires_in", 3600))
            self._token_expires = time.monotonic() + max(ttl - _TOKEN_LEEWAY, 30)
            return self._token

    # -- requests ---------------------------------------------------------

    def _check_policy(self, method: str, path: str) -> None:
        if not is_write(method, path):
            return
        if self.profile.read_only:
            raise PolicyError(
                f"La org {self.profile.name!r} está en modo solo lectura (read_only = true en orgs.toml); "
                f"se bloqueó {method} {path}."
            )
        if method.upper() == "DELETE" and not self.profile.allow_delete:
            raise PolicyError(
                f"Los borrados están deshabilitados para la org {self.profile.name!r} (allow_delete = false); "
                f"se bloqueó DELETE {path}."
            )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: Any = None,
        data: dict[str, Any] | None = None,
        tool: str | None = None,
        plan_preview: Any = None,
    ) -> Any:
        method = method.upper()
        self._check_policy(method, path)
        write = is_write(method, path)
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if plan := current_plan():
            # Plan mode (see confirm.py): reads hit Genesys, writes are only recorded.
            if write:
                plan.record(
                    self.profile.name, method, path, params, json_body if json_body is not None else data, plan_preview
                )
                return synthetic_write_response(path, json_body)
            if PLACEHOLDER_ID in path:
                return synthetic_read_response()
        correlation_id = str(uuid.uuid4())
        total_slept = 0.0
        reauthed = False
        last_detail = ""

        for attempt in range(_MAX_ATTEMPTS):
            token = await self._get_token()
            headers = {"Authorization": f"Bearer {token}", "ININ-Correlation-Id": correlation_id}
            try:
                async with self._limiter:
                    resp = await self._http.request(
                        method, path, params=params, json=json_body, files=files, data=data, headers=headers
                    )
            except httpx.TransportError as exc:
                last_detail = f"error de red: {exc}"
                if write:
                    self._audit(tool, method, path, None, correlation_id, error=last_detail)
                    raise GenesysError(
                        f"{method} {path}: {last_detail}. El resultado es incierto; verificá el estado antes de reintentar."
                    ) from exc
                delay = _backoff(attempt)
                if attempt == _MAX_ATTEMPTS - 1 or total_slept + delay > _BACKOFF_TOTAL_CAP:
                    break
                total_slept += delay
                await asyncio.sleep(delay)
                continue

            status = resp.status_code
            if status == 401 and not reauthed:
                reauthed = True
                await self._get_token(force=True)
                continue
            if status == 429 or (500 <= status <= 599 and not write):
                last_detail = f"HTTP {status}"
                delay = _retry_after(resp) or _backoff(attempt)
                if attempt == _MAX_ATTEMPTS - 1 or total_slept + delay > _BACKOFF_TOTAL_CAP:
                    break
                total_slept += delay
                await asyncio.sleep(delay)
                continue

            body = _parse_body(resp)
            if write:
                self._audit(tool, method, path, status, correlation_id)
            if status >= 400:
                raise GenesysAPIError(status, method, path, body, resp.headers.get("ININ-Correlation-Id", correlation_id))
            return body

        if write:
            self._audit(tool, method, path, None, correlation_id, error=last_detail)
        raise GenesysError(f"{method} {path}: falló tras reintentos ({last_detail}).")

    def _audit(self, tool, method, path, status, correlation_id, error=None) -> None:
        self.audit.write(
            org=self.profile.name,
            actor=self.actor,
            tool=tool,
            method=method,
            path=path,
            status=status,
            correlationId=correlation_id,
            confirmation=current_confirmation(),
            **({"error": error} if error else {}),
        )

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        """Collect `entities` across pageNumber/pageCount or nextUri/cursor pagination."""
        params = dict(params or {})
        params.setdefault("pageSize", page_size)
        items: list[dict] = []
        page = 1
        while True:
            if "cursor" not in params:
                params["pageNumber"] = page
            data = await self.request("GET", path, params=params)
            entities = (data or {}).get("entities", []) if isinstance(data, dict) else (data or [])
            items.extend(entities)
            if max_items is not None and len(items) >= max_items:
                return items[:max_items]
            if not isinstance(data, dict) or not entities:
                return items
            if data.get("cursor"):
                params.pop("pageNumber", None)
                params["cursor"] = data["cursor"]
                continue
            page_count = data.get("pageCount")
            if page_count is not None:
                if page >= page_count:
                    return items
            elif not data.get("nextUri"):
                return items
            page += 1

    async def download(self, url: str) -> bytes:
        """GET a presigned URL (no Genesys auth header: S3 rejects a second auth mechanism)."""
        async with httpx.AsyncClient(timeout=120.0, transport=self._transport) as raw:
            resp = await raw.get(url, follow_redirects=True)
        if resp.status_code >= 400:
            raise GenesysError(f"Descarga falló ({resp.status_code}): {resp.text[:300]}")
        return resp.content

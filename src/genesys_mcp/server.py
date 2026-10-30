"""Entry point.

  genesys-mcp            -> MCP server over stdio (what Claude Code runs)
  genesys-mcp check      -> test login + connectivity for every configured org
  genesys-mcp check lab  -> same, for one org
  genesys-mcp secrets export|import <archivo>  -> share client secrets as an encrypted file
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from . import tools  # noqa: F401  (registers tools on import)
from .app import client, mcp, settings
from .client import GenesysError
from .config import ConfigError


async def _check(orgs: list[str]) -> int:
    from .tools.orgs import test_connection

    failures = 0
    for name in orgs:
        try:
            info = await test_connection(org=name)
            print(json.dumps(info, ensure_ascii=False, indent=2))
        except (GenesysError, ConfigError) as exc:
            failures += 1
            print(f"[{name}] ERROR: {exc}", file=sys.stderr)
            continue
        await client(name).aclose()
    return 1 if failures else 0


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    args = sys.argv[1:]
    if args and args[0] == "secrets":
        from .secrets_bundle import main as secrets_main

        raise SystemExit(secrets_main(args[1:]))
    if args and args[0] == "check":
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8")
        try:
            names = args[1:] or list(settings().orgs)
        except ConfigError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(asyncio.run(_check(names)))
    if args and args[0] not in ("serve",):
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    mcp.run()


if __name__ == "__main__":
    main()

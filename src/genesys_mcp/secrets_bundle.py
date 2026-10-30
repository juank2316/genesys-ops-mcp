"""Share client secrets inside the team without a password manager.

  genesys-mcp secrets export <archivo>   -> encrypts the secrets named by client_secret_env in orgs.toml
  genesys-mcp secrets import <archivo>   -> decrypts and stores them as Windows *user* env vars

The file is encrypted with a passphrase (scrypt + Fernet/AES). Send the file through one channel
(Teams/OneDrive) and the passphrase through another (phone). Secret values are never printed.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .config import config_path

_FORMAT = "genesys-mcp-secrets/v1"
_SCRYPT_N = 2**15
_MIN_PASSPHRASE = 12


def _fernet(passphrase: str, salt: bytes, n: int) -> Fernet:
    key = Scrypt(salt=salt, length=32, n=n, r=8, p=1).derive(passphrase.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def _secret_env_names() -> dict[str, str]:
    """env var name -> org profile, for every org that reads its secret from an env var."""
    path = config_path()
    if path is None:
        raise SystemExit("No encontré orgs.toml.")
    tables = tomllib.loads(path.read_text(encoding="utf-8")).get("orgs") or {}
    return {t["client_secret_env"]: name for name, t in tables.items() if t.get("client_secret_env")}


def _read_user_env(name: str) -> str:
    """Prefer the persisted Windows user value: this process may predate a recent change."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            pass
    return os.environ.get(name, "")


def _write_user_env(values: dict[str, str]) -> None:
    import ctypes
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    # tell running apps (Explorer, new terminals) that the environment changed
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 0x2, 5000, None)


def export_bundle(target: Path) -> int:
    names = _secret_env_names()
    secrets, missing = {}, []
    for env_name, org in names.items():
        value = _read_user_env(env_name)
        if value:
            secrets[env_name] = value
        else:
            missing.append(f"{env_name} ({org})")
    if not secrets:
        print("No hay ningún secreto cargado en tus variables de entorno.", file=sys.stderr)
        return 1
    passphrase = getpass.getpass("Contraseña para cifrar (mín. 12 caracteres): ")
    if len(passphrase) < _MIN_PASSPHRASE:
        print("La contraseña es demasiado corta.", file=sys.stderr)
        return 1
    if getpass.getpass("Repetila: ") != passphrase:
        print("Las contraseñas no coinciden.", file=sys.stderr)
        return 1
    salt = os.urandom(16)
    payload = json.dumps({"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "secrets": secrets})
    bundle = {
        "format": _FORMAT,
        "kdf": {"name": "scrypt", "n": _SCRYPT_N, "salt": base64.b64encode(salt).decode()},
        "token": _fernet(passphrase, salt, _SCRYPT_N).encrypt(payload.encode("utf-8")).decode(),
    }
    target.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    print(f"Exporté {len(secrets)} secretos cifrados en {target.resolve()}")
    if missing:
        print("Sin valor en tus variables (no incluidos): " + ", ".join(missing))
    print("Mandá el archivo por un canal y la contraseña por OTRO (p.ej. por teléfono).")
    return 0


def import_bundle(source: Path) -> int:
    try:
        bundle = json.loads(source.read_text(encoding="utf-8"))
        if bundle.get("format") != _FORMAT:
            raise ValueError
        kdf = bundle["kdf"]
        salt = base64.b64decode(kdf["salt"])
    except (OSError, ValueError, KeyError):
        print(f"{source} no es un paquete de secretos de genesys-mcp.", file=sys.stderr)
        return 1
    passphrase = getpass.getpass("Contraseña del paquete: ")
    try:
        payload = json.loads(_fernet(passphrase, salt, int(kdf["n"])).decrypt(bundle["token"].encode()))
    except InvalidToken:
        print("Contraseña incorrecta (o archivo alterado).", file=sys.stderr)
        return 1
    secrets: dict[str, str] = payload["secrets"]
    if sys.platform != "win32":
        print("Por ahora la importación automática es solo para Windows.", file=sys.stderr)
        return 1
    _write_user_env(secrets)
    known = _secret_env_names()
    for name in sorted(secrets):
        print(f"  ok  {name}  ({known.get(name, 'org no listada en orgs.toml')})")
    without = sorted(set(known) - set(secrets))
    if without:
        print("Orgs sin secreto en el paquete (no vas a poder usarlas): " + ", ".join(f"{known[n]}" for n in without))
    print(f"\nListo: {len(secrets)} variables guardadas (paquete del {payload.get('created', '?')}).")
    print("Cerrá y volvé a abrir Claude para que el MCP las lea. Ya podés borrar el archivo del paquete.")
    return 0


def main(args: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    if len(args) != 2 or args[0] not in ("export", "import"):
        print(__doc__, file=sys.stderr)
        return 2
    action, file = args[0], Path(args[1]).expanduser()
    return export_bundle(file) if action == "export" else import_bundle(file)

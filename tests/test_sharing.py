import base64
import json
import tomllib

import pytest

from genesys_mcp import secrets_bundle
from genesys_mcp.config import PACKAGED_CONFIG


def test_packaged_client_list_has_no_secrets():
    """orgs.toml ships in the package and lives in Git: secrets must come from env vars only."""
    data = tomllib.loads(PACKAGED_CONFIG.read_text(encoding="utf-8"))
    for name, org in data["orgs"].items():
        assert "client_secret" not in org, f"{name}: usá client_secret_env, no client_secret"
        assert org.get("client_secret_env"), f"{name}: falta client_secret_env"
    raw = PACKAGED_CONFIG.read_text(encoding="utf-8")
    assert "G" not in "".join(w for w in raw.split() if "C__" in w)  # Genesys secrets look like G12C__...


def test_bundle_round_trip_and_wrong_passphrase(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_bundle, "_SCRYPT_N", 2**10)  # fast KDF for the test
    monkeypatch.setattr(secrets_bundle, "_secret_env_names", lambda: {"GENESYS_A": "a", "GENESYS_B": "b"})
    monkeypatch.setattr(secrets_bundle, "_read_user_env", lambda name: {"GENESYS_A": "s3cr3t-a"}.get(name, ""))
    passwords = iter(["correct horse battery", "correct horse battery"])
    monkeypatch.setattr(secrets_bundle.getpass, "getpass", lambda prompt="": next(passwords))
    target = tmp_path / "equipo.secrets"
    assert secrets_bundle.export_bundle(target) == 0
    assert "s3cr3t-a" not in target.read_text(encoding="utf-8")

    written = {}
    monkeypatch.setattr(secrets_bundle.sys, "platform", "win32")
    monkeypatch.setattr(secrets_bundle, "_write_user_env", written.update)
    monkeypatch.setattr(secrets_bundle.getpass, "getpass", lambda prompt="": "otra clave cualquiera")
    assert secrets_bundle.import_bundle(target) == 1 and written == {}
    monkeypatch.setattr(secrets_bundle.getpass, "getpass", lambda prompt="": "correct horse battery")
    assert secrets_bundle.import_bundle(target) == 0
    assert written == {"GENESYS_A": "s3cr3t-a"}


def test_short_passphrase_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_bundle, "_secret_env_names", lambda: {"GENESYS_A": "a"})
    monkeypatch.setattr(secrets_bundle, "_read_user_env", lambda name: "x")
    monkeypatch.setattr(secrets_bundle.getpass, "getpass", lambda prompt="": "corta")
    assert secrets_bundle.export_bundle(tmp_path / "f") == 1
    assert not (tmp_path / "f").exists()

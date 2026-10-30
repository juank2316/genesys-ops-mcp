import pytest

from genesys_mcp.config import ConfigError, load_settings
from genesys_mcp.regions import resolve_domain

LAB = '[orgs.lab]\nregion = "us-east-1"\nclient_id = "a"\nclient_secret = "b"\n'


def test_region_aliases():
    assert resolve_domain("us-east-1") == "mypurecloud.com"
    assert resolve_domain("https://api.mypurecloud.com/") == "mypurecloud.com"
    assert resolve_domain("sae1.pure.cloud") == "sae1.pure.cloud"
    assert resolve_domain("mx-central-1") == "mxc1.pure.cloud"
    with pytest.raises(ValueError):
        resolve_domain("marte-1")


@pytest.mark.parametrize(
    "region, api, apps",
    [
        ("us-east-1", "https://api.mypurecloud.com", "https://apps.mypurecloud.com"),
        ("sa-east-1", "https://api.sae1.pure.cloud", "https://apps.sae1.pure.cloud"),
        ("mx-central-1", "https://api.mxc1.pure.cloud", "https://apps.mxc1.pure.cloud"),
    ],
)
def test_script_upload_service_is_on_the_apps_host(region, api, apps):
    from genesys_mcp.config import OrgProfile

    profile = OrgProfile(name="x", region=region, client_id="i", client_secret="s")
    assert (profile.api_url, profile.apps_url) == (api, apps)


def test_load_toml_with_env_secret(tmp_path, monkeypatch):
    cfg = tmp_path / "orgs.toml"
    cfg.write_text(
        LAB + 'allow_delete = true\nenvironment = "lab"\n'
        '[orgs.cliente]\nregion = "sa-east-1"\nclient_id = "c"\nclient_secret_env = "CLI_SECRET"\nread_only = true\n'
        'display_name = "Cliente SA"\naliases = ["cli", "Cliente-BR"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg))
    monkeypatch.setenv("CLI_SECRET", "s3cr3t")
    s = load_settings()
    assert s.get("cliente").client_secret == "s3cr3t"
    assert s.get("cliente-br") is s.get("CLI") is s.get("cliente")
    assert s.get("cliente").api_url == "https://api.sae1.pure.cloud"
    assert s.get("cliente").environment == "prod"  # default when not declared
    assert s.get("cliente").label == "Cliente SA (cliente) · PROD · sa-east-1"
    assert s.get("lab").allow_delete and not s.get("lab").read_only
    assert "client_secret" not in s.get("cliente").public_view()
    with pytest.raises(ConfigError, match="Falta indicar el cliente"):
        s.get(None)
    with pytest.raises(ConfigError):
        s.get("otra")


@pytest.mark.parametrize(
    "content, message",
    [
        ('default = "lab"\n' + LAB, "default"),
        (LAB + LAB.replace("orgs.lab", "orgs.otra") + 'aliases = ["LAB"]\n', "único"),
        (LAB + 'environment = "produccion"\n', "environment"),
        (LAB + 'aliases = "lab2"\n', "aliases"),
    ],
)
def test_invalid_configs_are_rejected(tmp_path, monkeypatch, content, message):
    cfg = tmp_path / "orgs.toml"
    cfg.write_text(content, encoding="utf-8")
    monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg))
    with pytest.raises(ConfigError, match=message):
        load_settings()


def test_missing_env_secret_is_reported(tmp_path, monkeypatch):
    cfg = tmp_path / "orgs.toml"
    cfg.write_text('[orgs.x]\nregion = "us-east-1"\nclient_id = "a"\nclient_secret_env = "NOPE_XYZ"\n', encoding="utf-8")
    monkeypatch.setenv("GENESYS_MCP_CONFIG", str(cfg))
    monkeypatch.delenv("NOPE_XYZ", raising=False)
    with pytest.raises(ConfigError, match="NOPE_XYZ"):
        load_settings()

import asyncio
import json

import httpx
import pytest
from conftest import confirmed

from genesys_mcp.tools import telephony

TRUNKS = "/api/v2/telephony/providers/edges/trunkbasesettings"
DID, NO_DID = telephony.SUPPRESS_DID, telephony.SUPPRESS_NO_DID


def run(coro):
    return asyncio.run(coro)


def trunk(n: int, *, suppress: bool, name: str | None = None, trunk_type: str = "EXTERNAL", version: int = 1) -> dict:
    """A trunk shaped like the real API response (schema keys included, noisy metadata included)."""
    flag = {"type": "boolean", "value": {"default": False, "instance": suppress}}
    return {
        "id": f"00000000-0000-0000-0000-{n:012d}",
        "name": name or f"Telyaco outbound 5731104570{n:02d}",
        "version": version,
        "state": "active",
        "dateCreated": "2026-09-11T15:35:46.458Z",
        "modifiedBy": "u1",
        "selfUri": f"{TRUNKS}/{n}",
        "trunkType": trunk_type,
        "managed": False,
        "trunkMetabase": {"id": "external_sip_pcv_byoc_carrier.json", "name": "Generic BYOC Carrier"},
        "site": {"id": "s1", "name": "PureCloud Voice - AWS", "selfUri": "/s1"},
        "inboundSite": {"id": "s2", "name": "Argessa Inbound", "selfUri": "/s2"},
        "properties": {
            DID: dict(flag),
            NO_DID: {"type": "boolean", "value": {"default": False, "instance": suppress}},
            "trunk_maxCallRate": {"type": "string", "value": {"default": "40/5s", "instance": "40/5s"}},
            "trunk_maxCalls_reasonCode": {
                "type": "integer", "minimum": 400, "maximum": 700, "value": {"default": 603, "instance": 603}
            },
            "trunk_type": {"type": "string", "readonly": True, "value": {"default": "x", "instance": "x"}},
            "trunk_protocol": {"type": "string", "enum": ["SIP"], "value": {"default": "SIP", "instance": "SIP"}},
            "trunk_assertedIdentity_name": {"type": "string", "value": {"default": ""}},
        },
    }


def serve(fake, trunks: list[dict]):
    """Listing + one PUT route per trunk; PUT echoes the body with the version bumped."""
    fake.on("GET", TRUNKS, json_body={"entities": trunks, "pageCount": 1})
    for t in trunks:
        fake.on("PUT", f"{TRUNKS}/{t['id']}", lambda r: httpx.Response(200, json={**json.loads(r.content), "version": 2}))


def puts(fake):
    return [r for r in fake.calls if r.method == "PUT"]


@pytest.fixture
def telyaco(fake):
    trunks = [
        trunk(31, suppress=True), trunk(32, suppress=True),
        trunk(35, suppress=False), trunk(36, suppress=False), trunk(37, suppress=False),
        trunk(99, suppress=False, name="PureCloud Voice - AWS"),
        trunk(98, suppress=False, name="Tie TrunkBase", trunk_type="EDGE"),
    ]
    serve(fake, trunks)
    return trunks


# -- list_trunks / trunk_properties -------------------------------------------------------------


def test_list_trunks_shows_suppress_state_and_filters(fake, telyaco):
    out = run(telephony.list_trunks(org="lab", name_pattern="telyaco outbound*"))
    assert out["count"] == 5
    assert {t["name"][-2:]: t["suppress_username"] for t in out["trunks"]} == {
        "31": "Always", "32": "Always", "35": "Never", "36": "Never", "37": "Never",
    }
    assert run(telephony.list_trunks(org="lab", trunk_type="EDGE"))["count"] == 1
    with_props = run(telephony.list_trunks(org="lab", name_pattern="*35", properties=["trunk_maxCallRate"]))
    assert with_props["trunks"][0]["trunk_maxCallRate"] == "40/5s"


def test_suppress_state_partial(fake):
    t = trunk(1, suppress=True)
    t["properties"][NO_DID]["value"]["instance"] = False
    serve(fake, [t])
    assert run(telephony.list_trunks(org="lab"))["trunks"][0]["suppress_username"].startswith("Parcial")


def test_trunk_properties_discovery(fake, telyaco):
    out = run(telephony.trunk_properties(trunk="Telyaco outbound 573110457035", org="lab", contains="suppress"))
    assert [p["name"] for p in out["properties"]] == [DID, NO_DID]
    assert out["properties"][0]["current"] is False
    ro = run(telephony.trunk_properties(trunk="Telyaco outbound 573110457035", org="lab", contains="trunk_type"))
    assert ro["properties"][0]["readonly"] is True


# -- update_trunks: plan, execute -----------------------------------------------------------------


def test_bulk_suppress_username_plan_then_execute(fake, telyaco):
    kwargs = dict(org="lab", name_pattern="Telyaco outbound*", suppress_username="Always")
    plan = run(telephony.update_trunks(**kwargs))

    assert plan["status"] == "requiere_confirmacion" and puts(fake) == []
    # only the 3 that were in Never; the plan shows before/after, not the whole body
    assert [w["body"] for w in plan["cambios_planeados"]] == [
        json.dumps({"troncal": f"Telyaco outbound 5731104570{n}", "cambios": {
            DID: {"antes": False, "despues": True}, NO_DID: {"antes": False, "despues": True}}})
        for n in (35, 36, 37)
    ]
    assert plan["vista_previa"]["sin_cambios"] == ["Telyaco outbound 573110457031", "Telyaco outbound 573110457032"]
    assert any("reiniciar" in w for w in plan["advertencias"])

    out = run(telephony.update_trunks(**kwargs, confirmation_token=plan["confirmation_token"]))
    assert [u["name"][-2:] for u in out["updated"]] == ["35", "36", "37"] and out["updated"][0]["version"] == 2
    assert len(puts(fake)) == 3


def test_put_body_changes_only_the_flags(fake, telyaco):
    confirmed(telephony.update_trunks, org="lab", trunks=["Telyaco outbound 573110457035"], suppress_username="Always")
    (req,) = puts(fake)
    sent, original = json.loads(req.content), telyaco[2]

    assert sent["version"] == 1 and sent["name"] == original["name"]
    # read-only metadata dropped, references reduced to their id
    assert not {"dateCreated", "modifiedBy", "selfUri"} & set(sent)
    assert sent["site"] == {"id": "s1"} and sent["trunkMetabase"] == {"id": "external_sip_pcv_byoc_carrier.json"}
    # property schema noise dropped, values kept
    assert sent["properties"]["trunk_maxCallRate"] == {"value": {"default": "40/5s", "instance": "40/5s"}}
    assert sent["properties"]["trunk_assertedIdentity_name"] == {"value": {"default": ""}}
    assert sent["properties"][DID]["value"] == {"default": False, "instance": True}
    assert sent["properties"][NO_DID]["value"] == {"default": False, "instance": True}
    untouched = {k: v for k, v in sent["properties"].items() if k not in (DID, NO_DID)}
    assert untouched == {
        k: {"value": v["value"]} for k, v in original["properties"].items() if k not in (DID, NO_DID)
    }


def test_set_arbitrary_properties_with_validation(fake, telyaco):
    confirmed(telephony.update_trunks, org="lab", trunks=["Telyaco outbound 573110457036"],
              properties={"trunk_maxCallRate": "60/5s", "trunk_maxCalls_reasonCode": 604})
    props = json.loads(puts(fake)[0].content)["properties"]
    assert props["trunk_maxCallRate"]["value"]["instance"] == "60/5s"
    assert props["trunk_maxCalls_reasonCode"]["value"]["instance"] == 604


@pytest.mark.parametrize(
    "properties, message",
    [
        ({"trunk_maxCalls_reasonCode": 999}, "fuera del rango"),
        ({"trunk_maxCalls_reasonCode": "604"}, "espera un valor integer"),
        ({"trunk_protocol": "TCP"}, "no es válido"),
        ({"trunk_type": "y"}, "solo lectura"),
        ({"trunk_suppress_typo": True}, "Ninguna de las troncales"),
    ],
)
def test_invalid_changes_block_the_plan(fake, telyaco, properties, message):
    plan = run(telephony.update_trunks(org="lab", trunks=["Telyaco outbound 573110457035"], properties=properties))
    assert plan["status"] == "bloqueado" and message in plan["problemas"][0]
    assert puts(fake) == []


def test_nothing_to_change_is_sin_cambios(fake, telyaco):
    plan = run(telephony.update_trunks(org="lab", trunks=["Telyaco outbound 573110457031"], suppress_username="Always"))
    assert plan["status"] == "sin_cambios" and "confirmation_token" not in plan


def test_selection_errors(fake, telyaco):
    for kwargs, message in [
        ({"suppress_username": "Always"}, "Indicá qué troncales"),
        ({"name_pattern": "NoExiste*", "suppress_username": "Always"}, "Ninguna troncal coincide"),
        ({"trunks": ["Fantasma"], "suppress_username": "Always"}, "No encontré la troncal"),
        ({"trunks": ["x"]}, "Indicá qué cambiar"),
        ({"trunks": ["x"], "suppress_username": "Never", "properties": {DID: True}}, "usá solo uno"),
    ]:
        plan = run(telephony.update_trunks(org="lab", **kwargs))
        assert plan["status"] == "bloqueado" and message in plan["problemas"][0], (kwargs, plan)


def test_selection_by_id_and_pattern_combine_without_duplicates(fake, telyaco):
    out = confirmed(
        telephony.update_trunks, org="lab", suppress_username="Always",
        trunks=[telyaco[2]["id"], "PureCloud Voice - AWS"], name_pattern="*573110457035",
    )
    assert sorted(u["name"] for u in out["updated"]) == ["PureCloud Voice - AWS", "Telyaco outbound 573110457035"]


def test_trunks_without_the_property_are_skipped(fake, telyaco):
    edge = telyaco[-1]
    edge["properties"] = {"trunk_protocol": {"type": "string", "value": {"default": "SIP"}}}
    out = confirmed(telephony.update_trunks, org="lab", name_pattern="*", suppress_username="Always")
    assert {o["name"] for o in out["omitidas_sin_la_propiedad"]} == {"Tie TrunkBase"}
    assert len(out["updated"]) == 4  # 35, 36, 37 and the AWS one


def test_read_only_org_is_blocked(fake):
    serve(fake, [trunk(1, suppress=False)])
    plan = run(telephony.update_trunks(org="cliente", trunks=["Telyaco outbound 573110457001"], suppress_username="Always"))
    assert plan["status"] == "bloqueado" and "solo lectura" in plan["problemas"][0]
    assert puts(fake) == []


def test_selection_cap(fake):
    serve(fake, [trunk(n, suppress=False) for n in range(1, telephony.MAX_TRUNKS_PER_CALL + 2)])
    plan = run(telephony.update_trunks(org="lab", name_pattern="*", suppress_username="Always"))
    assert plan["status"] == "bloqueado" and "máximo" in plan["problemas"][0]

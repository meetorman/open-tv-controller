"""Home-network access (`access`): open by default, like a TV remote app;
`members` asks for a token at home too. The relay always needs a token."""

from __future__ import annotations

import json

from conftest import DEN, KITCHEN
from test_link import ISS, KEY, claims, mint
from test_link import rig as link_rig  # noqa: F401

from orman_tv_controller.link import RELAY_VIA
from orman_tv_controller.state import ControllerState


async def test_open_by_default_and_kept(tmp_path):
    state = ControllerState(tmp_path)
    assert state.access == "open"
    state.set_access("members")
    assert json.loads((tmp_path / "controller.json").read_text())["access"] == "members"
    assert ControllerState(tmp_path).access == "members"
    state.set_access("open")
    assert ControllerState(tmp_path).access == "open"


async def test_an_unknown_value_in_the_file_reads_as_open(tmp_path):
    ControllerState(tmp_path)
    raw = json.loads((tmp_path / "controller.json").read_text())
    (tmp_path / "controller.json").write_text(json.dumps({**raw, "access": "nobody"}))
    assert ControllerState(tmp_path).access == "open"


async def test_open_home_network_needs_no_token(rig):
    c = rig.client
    r = await c.get("/v1/tvs")
    assert r.status == 200 and {t["name"] for t in (await r.json())["tvs"]} == {"Den", "Kitchen"}
    r = await c.post(f"/v1/tvs/{DEN}/power", json={"power": "off"})
    assert r.status == 200
    r = await c.post(f"/v1/tvs/{DEN}/volume", json={"change": "up"})
    assert r.status == 200
    me = await (await c.get("/v1/controller")).json()
    assert me["access"] == "open"
    assert me["you"] == {"token": "", "scopes": ["control", "pair"], "issuer": "home"}


async def test_open_home_network_pairs_without_a_token(rig):
    c = rig.client
    for _ in range(2):  # AirPlay, then Companion
        r = await c.post(f"/v1/tvs/{KITCHEN}/pair")
        assert r.status == 200 and (await r.json())["needs_pin"]
        r = await c.post(f"/v1/tvs/{KITCHEN}/pair/pin", json={"pin": "1234"})
        assert r.status == 200
    assert (await r.json())["done"]
    r = await c.post(f"/v1/tvs/{KITCHEN}/unpair")
    assert r.status == 200 and (await r.json())["paired"] is False


async def test_open_still_keeps_admin_to_the_admin_token(rig):
    for method, path in [
        ("GET", "/v1/tokens"),
        ("POST", "/v1/tokens"),
        ("GET", "/v1/registration"),
        ("POST", "/v1/registration/code"),
        ("GET", "/v1/access"),
        ("PUT", "/v1/access"),
    ]:
        r = await rig.client.request(method, path, json={"access": "members"})
        assert r.status == 403, path
    assert rig.state.access == "open"


async def test_open_with_a_stale_token_still_works_at_home(rig):
    r = await rig.client.get("/v1/tvs", headers={"Authorization": "Bearer otc_gone.gone"})
    assert r.status == 200
    # a control token gets pair too, the same as no token
    me = await (await rig.client.get("/v1/controller", headers=rig.h())).json()
    assert me["you"]["scopes"] == ["control", "pair"] and me["you"]["issuer"] == "local"
    # and the admin token keeps admin
    me = await (await rig.client.get("/v1/controller", headers=rig.ha())).json()
    assert "admin" in me["you"]["scopes"]


async def test_open_is_not_for_anything_passed_on(rig):
    for header in ("X-Forwarded-For", "X-Real-IP", "Forwarded", "Via"):
        value = RELAY_VIA if header == "Via" else "203.0.113.9"
        r = await rig.client.get("/v1/tvs", headers={header: value})
        assert r.status == 401, header
        r = await rig.client.get("/v1/tvs", headers={header: value, **rig.h()})
        assert r.status == 200, header


async def test_members_needs_a_token_at_home(rig):
    rig.state.set_access("members")
    for method, path in [
        ("GET", "/v1/controller"),
        ("GET", "/v1/tvs"),
        ("POST", f"/v1/tvs/{DEN}/power"),
        ("POST", f"/v1/tvs/{KITCHEN}/pair"),
    ]:
        r = await rig.client.request(method, path)
        assert r.status == 401, path
        assert (await r.json())["error"]["code"] == "unauthorized"
    r = await rig.client.get("/v1/tvs", headers=rig.h())
    assert r.status == 200
    # a local control token doesn't pair in members mode
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair", headers=rig.h())
    assert r.status == 403


async def test_admin_api_sets_access(rig):
    c = rig.client
    r = await c.get("/v1/access", headers=rig.ha())
    assert await r.json() == {"access": "open", "modes": ["open", "members"]}
    r = await c.put("/v1/access", json={"access": "members"}, headers=rig.ha())
    assert r.status == 200 and (await r.json())["access"] == "members"
    assert ControllerState(rig.state.folder).access == "members"
    assert (await c.get("/v1/tvs")).status == 401
    for bad in ({"access": "everyone"}, {}, {"access": None}):
        r = await c.put("/v1/access", json=bad, headers=rig.ha())
        assert r.status == 400
    r = await c.put("/v1/access", json={"access": "open"}, headers=rig.h())
    assert r.status == 403
    r = await c.put("/v1/access", json={"access": "open"}, headers=rig.ha())
    assert (await r.json())["access"] == "open"
    assert (await c.get("/v1/tvs")).status == 200


async def test_discovery_says_which(rig):
    body = await (await rig.client.get("/discovery")).json()
    assert body["access"] == "open"
    rig.state.set_access("members")
    body = await (await rig.client.get("/discovery")).json()
    assert body["access"] == "members"


async def test_the_relay_always_needs_a_members_token(link_rig):  # noqa: F811
    provider, state, link, client, admin, base = link_rig
    await provider.next("hello")
    state.registered(ISS, "cred", KEY)
    assert state.access == "open"

    async def relay(i: int, path: str, authorization: str = "") -> int:
        await provider.ws.send_json(
            {
                "type": "request",
                "id": i,
                "method": "GET",
                "path": path,
                "authorization": authorization,
            }
        )
        while True:
            msg = await provider.next("response")
            if msg["id"] == i:
                return msg["status"]

    assert await relay(1, "/v1/tvs") == 401
    assert await relay(2, "/v1/tvs", "Bearer otp1.bad.token") == 401
    token = mint(KEY, claims(state.id))
    assert await relay(3, "/v1/tvs", f"Bearer {token}") == 200

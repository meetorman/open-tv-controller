"""A member pairs and unpairs the controller's TVs from the app, with the token
the provider minted for them, but only on a request straight from the home
network. Admin (the page) pairs as before."""

from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from conftest import DEN, KITCHEN
from test_link import ISS, KEY, claims, mint

from orman_tv_controller import api
from orman_tv_controller import capabilities as caps


def _member(rig) -> dict[str, str]:
    rig.state.registered(ISS, "cred", KEY)
    return rig.h(mint(KEY, claims(rig.state.id)))


def _from(remote: str):
    @web.middleware
    async def mw(request, handler):
        return await handler(request.clone(remote=remote))

    return mw


async def test_a_member_pairs_at_home(rig):
    h = _member(rig)
    r = await rig.client.get("/v1/controller", headers=h)
    assert (await r.json())["you"]["scopes"] == ["control", "pair"]
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair", headers=h)
    step = await r.json()
    assert r.status == 200 and step["step"] == "airplay" and step["needs_pin"]
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair/pin", json={"pin": "1234"}, headers=h)
    assert (await r.json())["remaining"] == ["companion"]
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair", headers=h)
    assert (await r.json())["step"] == "companion"
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair/pin", json={"pin": "1234"}, headers=h)
    assert (await r.json())["done"]
    tv = await (await rig.client.get(f"/v1/tvs/{KITCHEN}", headers=h)).json()
    assert tv["paired"] and caps.POWER in tv["capabilities"]


async def test_a_wrong_pin_and_a_cancel(rig):
    h = _member(rig)
    await rig.client.post(f"/v1/tvs/{KITCHEN}/pair", headers=h)
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair/pin", json={"pin": "9999"}, headers=h)
    assert r.status == 422 and (await r.json())["error"]["code"] == "pairing_failed"
    await rig.client.post(f"/v1/tvs/{KITCHEN}/pair", headers=h)
    r = await rig.client.delete(f"/v1/tvs/{KITCHEN}/pair", headers=h)
    assert (await r.json())["cancelled"]
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair/pin", json={"pin": "1234"}, headers=h)
    assert r.status == 400


async def test_a_member_unpairs_at_home(rig):
    h = _member(rig)
    await rig.client.post(f"/v1/tvs/{DEN}/power", json={"power": "on"}, headers=h)
    r = await rig.client.post(f"/v1/tvs/{DEN}/unpair", headers=h)
    assert (await r.json()) == {"tv": DEN, "paired": False, "to_pair": ["airplay", "companion"]}
    r = await rig.client.post(f"/v1/tvs/{DEN}/power", json={"power": "off"}, headers=h)
    assert r.status == 409 and (await r.json())["error"]["code"] == "not_paired"
    tv = await (await rig.client.get(f"/v1/tvs/{DEN}", headers=h)).json()
    assert not tv["paired"] and tv["capabilities"] == []


async def test_a_sleeping_tv_says_so(rig):
    h = _member(rig)
    await rig.client.get("/v1/tvs", headers=h)  # found while awake
    rig.driver.tvs["FAKE-2"].reachable = False
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair", headers=h)
    assert r.status == 502 and (await r.json())["error"]["code"] == "tv_unreachable"


async def test_not_through_a_proxy(rig):
    h = {**_member(rig), "X-Forwarded-For": "203.0.113.9"}
    for method, path in [
        ("POST", f"/v1/tvs/{KITCHEN}/pair"),
        ("POST", f"/v1/tvs/{KITCHEN}/pair/pin"),
        ("DELETE", f"/v1/tvs/{KITCHEN}/pair"),
        ("POST", f"/v1/tvs/{DEN}/unpair"),
    ]:
        r = await rig.client.request(method, path, headers=h, json={"pin": "1234"})
        assert r.status == 403, path
        assert (await r.json())["error"]["code"] == "home_network_only"
    # control still works from anywhere
    r = await rig.client.get(f"/v1/tvs/{DEN}/power", headers=h)
    assert r.status == 200
    assert rig.driver.tvs["FAKE-1"].paired


async def test_not_from_the_internet(rig):
    h = _member(rig)
    app = api.build_app(rig.controller, rig.state, rig.tokens)
    app.middlewares.insert(0, _from("203.0.113.9"))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        r = await client.post(f"/v1/tvs/{KITCHEN}/pair", headers=h)
        assert r.status == 403 and (await r.json())["error"]["code"] == "home_network_only"
        # the page's admin token is the operator's: it pairs wherever it is used
        r = await client.post(f"/v1/tvs/{KITCHEN}/pair", headers=rig.ha())
        assert r.status == 200
    finally:
        await client.close()


async def test_a_local_control_token_does_not_pair(rig):
    rig.state.set_access(
        "members"
    )  # tokens at home too (the default, open, lets the home network in)
    for method, path in [("POST", f"/v1/tvs/{KITCHEN}/pair"), ("POST", f"/v1/tvs/{DEN}/unpair")]:
        r = await rig.client.request(method, path, headers=rig.h())
        assert r.status == 403 and (await r.json())["error"]["code"] == "forbidden"


async def test_admin_unpairs(rig):
    r = await rig.client.post(f"/v1/tvs/{DEN}/unpair", headers=rig.ha())
    assert not (await r.json())["paired"]
    assert not rig.driver.tvs["FAKE-1"].paired

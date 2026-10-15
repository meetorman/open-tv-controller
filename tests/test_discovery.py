from __future__ import annotations

import time

import pytest
from aiohttp import web

from orman_tv_controller import api, bonjour, lan
from orman_tv_controller.state import ControllerState


@pytest.mark.parametrize(
    "remote",
    [
        "192.168.1.40",
        "10.1.2.3",
        "172.20.0.5",
        "169.254.9.9",
        "127.0.0.1",
        "::1",
        "fe80::1%en0",
        "fd12:3456::7",
        "::ffff:192.168.1.9",
    ],
)
def test_lan_sources(remote):
    assert lan.is_lan_address(remote)


@pytest.mark.parametrize(
    "remote", ["8.8.8.8", "100.64.1.1", "172.32.0.1", "2001:db8::1", "::ffff:1.1.1.1", "", None]
)
def test_other_sources(remote):
    assert not lan.is_lan_address(remote)


def test_a_proxied_request_is_not_lan():
    assert lan.is_lan_request("127.0.0.1", {})
    assert not lan.is_lan_request("127.0.0.1", {"X-Forwarded-For": "203.0.113.9"})
    assert not lan.is_lan_request("192.168.1.2", {"Forwarded": "for=203.0.113.9"})


def _from(remote: str):
    @web.middleware
    async def mw(request, handler):
        return await handler(request.clone(remote=remote))

    return mw


async def test_discovery_needs_no_token_on_the_lan(rig):
    r = await rig.client.get("/discovery")
    assert r.status == 200
    assert r.headers["Cache-Control"] == "no-store"
    body = await r.json()
    reg = rig.state.registration
    assert body["id"] == rig.state.id and body["name"] == "Test controller"
    assert body["api"] == 1
    assert body["registration"] == {
        "state": "unregistered",
        "code": reg.code,
        "expires_at": int(reg.expires_at),
    }
    # names only: no ids, addresses or capabilities
    assert body["tvs"] == [{"name": "Den"}, {"name": "Kitchen"}]


async def test_discovery_refuses_a_proxied_request(rig):
    r = await rig.client.get("/discovery", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "forbidden"


async def test_discovery_refuses_the_internet(rig):
    from aiohttp.test_utils import TestClient, TestServer

    app = api.build_app(rig.controller, rig.state, rig.tokens)
    app.middlewares.insert(0, _from("203.0.113.9"))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        r = await client.get("/discovery")
        assert r.status == 403
        assert rig.state.registration.code not in await r.text()
    finally:
        await client.close()


async def test_discovery_renews_a_code_about_to_expire(rig):
    rig.state.registration.expires_at = time.time() + 30
    old = rig.state.registration.code
    body = await (await rig.client.get("/discovery")).json()
    assert body["registration"]["code"] != old
    assert body["registration"]["expires_at"] > time.time() + 600


def test_txt_says_whether_it_is_registered():
    assert bonjour.txt("ctl_x", "0.3.0")["reg"] == "0"
    info = bonjour.service_info("C", "ctl_0123456789abcdef", 8820, "0.3.0", [], registered=True)
    assert info.decoded_properties["reg"] == "1"


async def test_advertiser_follows_the_registration():
    info = bonjour.service_info("C", "ctl_0123456789abcdef", 8820, "0.3.0", ["192.0.2.5"])
    adv = bonjour.Advertiser(info)  # not started: only the record changes
    adv.set_registered(True)
    assert adv.info.decoded_properties == {
        "id": "ctl_0123456789abcdef",
        "api": "1",
        "v": "0.3.0",
        "reg": "1",
    }
    assert adv.info.port == 8820 and adv.info.parsed_addresses() == ["192.0.2.5"]


def test_state_tells_listeners_when_registration_changes(tmp_path):
    state = ControllerState(tmp_path)
    seen = []
    state.on_registration_change.append(lambda: seen.append(state.registration.state))
    state.unregistered()  # no change: nothing said
    state.registered("https://p.example", "cred", "key")
    state.unregistered()
    assert seen == ["registered", "unregistered"]

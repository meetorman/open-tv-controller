"""Phase 2: provider-minted tokens, and the link to a pretend TV control provider
(registration, the relay, unregistering). No network beyond the loopback."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import pathlib
import time

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from conftest import DEN

from orman_tv_controller import api, auth, cli
from orman_tv_controller.api import build_app
from orman_tv_controller.drivers.fake import FakeDriver
from orman_tv_controller.link import ProviderLink, connect_url
from orman_tv_controller.service import Controller, tv_id
from orman_tv_controller.state import ControllerState


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def mint(key: str, claims: dict) -> str:
    """What a provider does: HMAC-SHA256 over the claims with the controller's key."""
    body = b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    return f"otp1.{body}.{b64(hmac.new(raw, f'otp1.{body}'.encode(), hashlib.sha256).digest())}"


KEY = b64(os.urandom(32))
ISS = "https://provider.example/v1/p/tvcontrol/tvcontrol"


def claims(aud: str, **over) -> dict:
    now = int(time.time())
    return {
        "iss": ISS,
        "aud": aud,
        "sub": "m_one",
        "scope": ["control"],
        "iat": now,
        "exp": now + 300,
        "jti": "j1",
        **over,
    }


def test_connect_url():
    assert connect_url("https://p.example/v1/p/tvcontrol/tvcontrol/") == (
        "wss://p.example/v1/p/tvcontrol/tvcontrol/connect"
    )
    assert connect_url("http://127.0.0.1:9/x").startswith("ws://127.0.0.1:9/x/connect")


async def test_provider_tokens(tmp_path):
    state = ControllerState(tmp_path)
    verifier = auth.ProviderTokens(state)
    token = mint(KEY, claims(state.id))
    assert await verifier.verify(token) is None  # not registered yet
    state.registered(ISS, "cred", KEY)
    grant = await verifier.verify(token)
    assert grant.name == "m_one" and grant.allows("control") and not grant.allows("admin")
    assert grant.issuer == ISS
    for bad in (
        mint(KEY, claims(state.id, exp=int(time.time()) - 1)),
        mint(KEY, claims("ctl_ffffffffffffffff")),
        mint(KEY, claims(state.id, iss="https://other.example")),
        mint(b64(os.urandom(32)), claims(state.id)),
        mint(KEY, claims(state.id, scope=["admin"])),  # a provider never grants admin
        "otp1.x.y",
    ):
        assert await verifier.verify(bad) is None
    state.unregistered()
    assert await verifier.verify(token) is None


class FakeProvider:
    """A provider's /connect: records what the controller says, lets the test speak."""

    def __init__(self) -> None:
        self.messages: asyncio.Queue = asyncio.Queue()
        self.ws: web.WebSocketResponse | None = None
        self.auth: list[str | None] = []
        self.connected = asyncio.Event()

    async def connect(self, request: web.Request) -> web.WebSocketResponse:
        self.auth.append(request.headers.get("Authorization"))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        self.connected.set()
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await self.messages.put(json.loads(msg.data))
        return ws

    async def next(self, kind: str) -> dict:
        while True:
            msg = await asyncio.wait_for(self.messages.get(), 5)
            if msg.get("type") == kind:
                return msg


@pytest.fixture
async def rig(tmp_path):
    provider = FakeProvider()
    papp = web.Application()
    papp.router.add_get("/p/connect", provider.connect)
    pserver = TestServer(papp)
    await pserver.start_server()

    state = ControllerState(tmp_path, name="Den controller")
    tokens = auth.LocalTokens(tmp_path / "tokens.json")
    admin, _ = tokens.mint("setup", [auth.ADMIN])
    controller = Controller([FakeDriver()])
    base = str(pserver.make_url("/p")).rstrip("/")
    link = ProviderLink(state, base, "http://unset", controller.list_tvs, heartbeat_s=5)
    client = TestClient(TestServer(build_app(controller, state, tokens, link=link)))
    await client.start_server()
    link.local_base = str(client.make_url("")).rstrip("/")
    link.start()
    await asyncio.wait_for(provider.connected.wait(), 5)
    try:
        yield provider, state, link, client, admin, base
    finally:
        await client.close()
        await pserver.close()


async def test_register_relay_unregister(rig):
    provider, state, link, client, admin, base = rig
    hello = await provider.next("hello")
    assert hello["controller_id"] == state.id and hello["code"] == state.registration.code
    assert [t["name"] for t in hello["tvs"]] == ["Den", "Kitchen"]
    assert provider.auth == [None]  # unregistered: no credential
    await provider.ws.send_json({"type": "welcome", "registered": False})
    old_code = state.registration.code
    await provider.ws.send_json(
        {
            "type": "registered",
            "provider": base,
            "controller_id": state.id,
            "credential": "cred-1",
            "key": KEY,
        }
    )
    update = await provider.next("update")
    assert state.registration.state == "registered" and state.registration.provider == base
    assert update["code"] == state.registration.code != old_code  # the code was used: a new one

    # the page sees it
    r = await client.get("/v1/registration", headers={"Authorization": f"Bearer {admin}"})
    reg = await r.json()
    assert reg["state"] == "registered" and reg["link"]["status"] == "connected"

    # a member's call through the relay, with a token the provider minted
    token = mint(KEY, {**claims(state.id), "iss": base})
    await provider.ws.send_json(
        {
            "type": "request",
            "id": 7,
            "method": "POST",
            "path": f"/v1/tvs/{DEN}/power",
            "query": "",
            "authorization": f"Bearer {token}",
            "body": {"power": "on"},
        }
    )
    answer = await provider.next("response")
    assert answer == {
        "type": "response",
        "id": 7,
        "status": 200,
        "body": {"tv": DEN, "power": "on"},
    }
    # the same token works directly on the LAN
    r = await client.get(f"/v1/tvs/{DEN}/power", headers={"Authorization": f"Bearer {token}"})
    assert (await r.json())["power"] == "on"
    # a token that isn't good is refused down the relay too, and admin calls stay local-only
    await provider.ws.send_json(
        {
            "type": "request",
            "id": 8,
            "method": "GET",
            "path": "/v1/tokens",
            "authorization": f"Bearer {token}",
        }
    )
    assert (await provider.next("response"))["status"] == 403
    await provider.ws.send_json(
        {
            "type": "request",
            "id": 9,
            "method": "GET",
            "path": "/v1/tvs",
            "authorization": "Bearer otp1.bad.token",
        }
    )
    assert (await provider.next("response"))["status"] == 401
    # pairing needs the home network: never through the relay, even with a member's token
    for i, (method, path) in enumerate(
        [("POST", f"/v1/tvs/{DEN}/pair"), ("DELETE", f"/v1/tvs/{DEN}/pair")], start=20
    ):
        await provider.ws.send_json(
            {
                "type": "request",
                "id": i,
                "method": method,
                "path": path,
                "authorization": f"Bearer {token}",
            }
        )
        answer = await provider.next("response")
        assert answer["status"] == 403 and answer["body"]["error"]["code"] == "home_network_only"
    # paired at home: the provider's list (what members see) gets the TV's capabilities
    h = {"Authorization": f"Bearer {token}"}
    kitchen = tv_id("fake", "FAKE-2")
    for _ in range(2):
        await client.post(f"/v1/tvs/{kitchen}/pair", headers=h)
        r = await client.post(f"/v1/tvs/{kitchen}/pair/pin", json={"pin": "1234"}, headers=h)
    assert (await r.json())["done"]
    update = await provider.next("update")
    caps_now = {t["name"]: t["capabilities"] for t in update["tvs"]}
    assert "power" in caps_now["Kitchen"]
    await client.post(f"/v1/tvs/{kitchen}/unpair", headers=h)
    update = await provider.next("update")
    assert {t["name"]: t["capabilities"] for t in update["tvs"]}["Kitchen"] == []
    await provider.ws.send_json({"type": "request", "id": 10, "method": "GET", "path": "/admin"})
    assert (await provider.next("response"))["status"] == 404

    # nobody has it any more
    await provider.ws.send_json({"type": "unregistered"})
    await provider.next("hello")  # pinned by the operator: it connects again, unregistered
    assert provider.auth[-1] is None
    assert state.registration.state == "unregistered"
    state.set_access("members")  # open lets the home network in with any token
    r = await client.get("/v1/tvs", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 401


async def test_reconnects_with_its_credential(rig):
    provider, state, link, client, admin, base = rig
    await provider.next("hello")
    await provider.ws.send_json(
        {
            "type": "registered",
            "provider": base,
            "controller_id": state.id,
            "credential": "cred-1",
            "key": KEY,
        }
    )
    await provider.next("update")
    provider.connected.clear()
    await provider.ws.close()
    await asyncio.wait_for(provider.connected.wait(), 10)
    await provider.next("hello")
    assert provider.auth[-1] == "Bearer cred-1"


async def test_leaving_the_provider(rig):
    provider, state, link, client, admin, base = rig
    await provider.next("hello")
    await provider.ws.send_json(
        {
            "type": "registered",
            "provider": base,
            "controller_id": state.id,
            "credential": "cred-1",
            "key": KEY,
        }
    )
    await provider.next("update")
    r = await client.delete("/v1/registration", headers={"Authorization": f"Bearer {admin}"})
    assert (await r.json())["state"] == "unregistered"
    assert (await provider.next("unregister"))["type"] == "unregister"
    # a registered controller can't switch providers; an unregistered one can
    r = await client.put(
        "/v1/registration/provider",
        json={"provider": "ftp://x"},
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert r.status == 400


async def test_new_code_reaches_the_provider(rig):
    provider, state, link, client, admin, base = rig
    await provider.next("hello")
    r = await client.post("/v1/registration/code", headers={"Authorization": f"Bearer {admin}"})
    code = (await r.json())["code"]
    assert (await provider.next("update"))["code"] == code


# -- the provider comes from the app that found the controller ---------------------


@pytest.fixture
async def quiet(tmp_path, monkeypatch):
    """A controller as it starts fresh: no --provider, so no provider at all."""
    provider = FakeProvider()
    papp = web.Application()
    papp.router.add_get("/p/connect", provider.connect)
    pserver = TestServer(papp)
    await pserver.start_server()
    dialed: list[str] = []
    real = aiohttp.ClientSession.ws_connect

    def ws_connect(self, url, *a, **kw):
        dialed.append(str(url))
        return real(self, url, *a, **kw)

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", ws_connect)

    state = ControllerState(tmp_path, name="Den controller")
    tokens = auth.LocalTokens(tmp_path / "tokens.json")
    controller = Controller([FakeDriver()])
    link = ProviderLink(state, None, "http://unset", controller.list_tvs, heartbeat_s=5)
    app = build_app(controller, state, tokens, link=link)
    app[api.CONNECT_WAIT] = 5.0
    client = TestClient(TestServer(app))
    await client.start_server()
    link.local_base = str(client.make_url("")).rstrip("/")
    link.start()
    base = str(pserver.make_url("/p")).rstrip("/")
    try:
        yield provider, state, link, client, base, dialed
    finally:
        await client.close()
        await pserver.close()


def test_no_built_in_provider(monkeypatch):
    src = pathlib.Path(__file__).parent.parent / "src"
    for f in src.rglob("*"):
        if f.is_file() and f.suffix in (".py", ".html"):
            assert "askorman" not in f.read_text().lower(), f
    monkeypatch.delenv("ORMAN_TVCTL_PROVIDER", raising=False)
    assert cli.parser().parse_args(["serve"]).provider == ""


async def test_a_fresh_controller_dials_nobody(quiet):
    provider, state, link, client, base, dialed = quiet
    await asyncio.sleep(0.5)
    assert dialed == [] and not provider.connected.is_set()
    assert link.provider is None and link.public()["status"] == "off"
    body = await (await client.get("/discovery")).json()
    assert body["link"] == {"provider": None, "status": "off", "error": None}
    await asyncio.sleep(0.2)
    assert dialed == []


async def test_the_app_names_the_provider(quiet, tmp_path):
    provider, state, link, client, base, dialed = quiet
    call = asyncio.create_task(client.post("/discovery/provider", json={"provider": base + "/"}))
    hello = await provider.next("hello")
    assert not call.done()  # it answers once the provider has welcomed the link
    await provider.ws.send_json({"type": "welcome", "registered": False, "provider": base})
    r = await asyncio.wait_for(call, 5)
    assert r.status == 200
    body = await r.json()
    assert body["link"] == {"provider": base, "status": "connected", "error": None}
    assert body["registration"]["state"] == "unregistered"
    assert dialed == [base.replace("http://", "ws://") + "/connect"]
    assert hello["code"] == body["registration"]["code"] == state.registration.code
    assert provider.auth == [None]
    # kept: a restart goes back to the same provider
    assert ControllerState(tmp_path).registration.chosen_provider == base

    # the provider registers it; a registered controller keeps that provider
    await provider.ws.send_json(
        {
            "type": "registered",
            "provider": base,
            "controller_id": state.id,
            "credential": "cred-1",
            "key": KEY,
        }
    )
    await provider.next("update")
    again = ControllerState(tmp_path).registration
    assert again.state == "registered" and again.provider == base
    r = await client.post("/discovery/provider", json={"provider": "https://other.example/p"})
    assert r.status == 409 and (await r.json())["error"]["code"] == "registered"
    r = await client.post("/discovery/provider", json={"provider": base})
    assert r.status == 200

    # the last member lets it go: it forgets the provider and goes quiet
    await provider.ws.send_json({"type": "unregistered"})
    for _ in range(50):
        if link.public()["status"] == "off":
            break
        await asyncio.sleep(0.1)
    assert link.provider is None and link.public()["status"] == "off"
    assert ControllerState(tmp_path).registration.chosen_provider is None
    n = len(dialed)
    await asyncio.sleep(0.5)
    assert len(dialed) == n


async def test_the_provider_call_is_for_the_home_network_only(quiet):
    provider, state, link, client, base, dialed = quiet
    r = await client.post(
        "/discovery/provider", json={"provider": base}, headers={"X-Forwarded-For": "203.0.113.9"}
    )
    assert r.status == 403
    for bad in ({}, {"provider": "ftp://x"}, {"provider": 7}):
        r = await client.post("/discovery/provider", json=bad)
        assert r.status == 400
    await asyncio.sleep(0.2)
    assert dialed == [] and link.provider is None

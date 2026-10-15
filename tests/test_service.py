from __future__ import annotations

import asyncio
import time

import pytest
from conftest import DEN

from orman_tv_controller import auth, errors
from orman_tv_controller.drivers.fake import FakeDriver
from orman_tv_controller.service import Controller, match_app


async def test_connection_kept_then_closed_when_idle():
    driver = FakeDriver()
    ctl = Controller([driver], keepalive_s=0.05)
    await ctl.power(DEN)
    await ctl.volume(DEN)
    assert driver.connects == 1  # one connection for both calls
    await asyncio.sleep(0.15)
    assert driver.closes == 1
    await ctl.power(DEN)
    assert driver.connects == 2
    await ctl.close()


async def test_lost_connection_is_retried_once():
    driver = FakeDriver()
    ctl = Controller([driver], keepalive_s=30)
    await ctl.power(DEN)
    fake = driver.tvs["FAKE-1"]
    stale = ctl._entries[DEN].session
    fake.reachable = False
    with pytest.raises(errors.Unreachable):
        await ctl.power(DEN)
    assert ctl._entries[DEN].session is None and stale.closed
    fake.reachable = True
    assert (await ctl.power(DEN))["power"] == "off"
    await ctl.close()


async def test_tv_missing_from_a_later_scan_stays_listed():
    driver = FakeDriver()
    ctl = Controller([driver], scan_ttl_s=0)
    await ctl.list_tvs()
    driver.tvs["FAKE-2"].reachable = False
    listed = {t["name"]: t for t in await ctl.list_tvs(refresh=True)}
    assert listed["Kitchen"]["reachable"] is False
    await ctl.close()


async def test_tv_addresses_limit_the_scan():
    driver = FakeDriver()
    ctl = Controller([driver], tv_addresses=["192.0.2.11"])
    assert [t["name"] for t in await ctl.list_tvs()] == ["Kitchen"]
    await ctl.close()


async def test_slow_tv_times_out():
    driver = FakeDriver()

    async def slow(_tv):
        await asyncio.sleep(1)

    driver.connect = slow
    ctl = Controller([driver], call_timeout_s=0.05)
    with pytest.raises(errors.TvTimeout):
        await ctl.power(DEN)


def test_match_app():
    apps = [
        {"id": "a", "name": "YouTube"},
        {"id": "b", "name": "YouTube Kids"},
        {"id": "c", "name": "Disney+"},
    ]
    assert match_app(apps, "youtube")["id"] == "a"
    assert match_app(apps, "disney plus")["id"] == "c"
    with pytest.raises(errors.BadRequest):
        match_app(apps, "you")


async def test_local_token_expiry(tmp_path):
    tokens = auth.LocalTokens(tmp_path / "t.json")
    token, _ = tokens.mint("short", [auth.CONTROL], ttl_s=0.01)
    time.sleep(0.02)
    assert await tokens.verify(token) is None
    token, row = tokens.mint("long", [auth.CONTROL])
    grant = await tokens.verify(token)
    assert grant.token_id == row["id"] and grant.allows("control") and not grant.allows("admin")
    # kept hashed, survives a restart
    again = auth.LocalTokens(tmp_path / "t.json")
    assert token not in (tmp_path / "t.json").read_text()
    assert (await again.verify(token)).name == "long"
    assert (tmp_path / "t.json").stat().st_mode & 0o777 == 0o600


async def test_another_verifier_slots_in():
    class ProviderTokens:
        async def verify(self, token):
            if token == "provider-minted":
                return auth.Grant(
                    "p1",
                    "member",
                    frozenset({auth.CONTROL}),
                    "https://provider.example",
                    time.time() + 60,
                )
            return None

    a = auth.Authenticator([ProviderTokens()])
    grant = await a.grant("Bearer provider-minted")
    assert grant.issuer == "https://provider.example"
    assert await a.grant("Bearer other") is None
    assert await a.grant(None) is None


async def test_tokens_minted_elsewhere_are_seen(tmp_path):
    server = auth.LocalTokens(tmp_path / "t.json")
    cli = auth.LocalTokens(tmp_path / "t.json")
    token, row = cli.mint("from the cli", [auth.CONTROL])
    assert (await server.verify(token)).token_id == row["id"]
    cli.revoke(row["id"])
    assert await server.verify(token) is None


async def test_unavailable_on_a_kept_connection_asks_a_fresh_one():
    driver = FakeDriver()
    ctl = Controller([driver], keepalive_s=30)
    await ctl.power(DEN)
    stale = ctl._entries[DEN].session
    stale.is_available = lambda cap: False  # what it learnt while the TV slept
    assert (await ctl.apps(DEN))["apps"]
    assert driver.connects == 2 and stale.closed
    # a fresh connection that says no is the answer
    with pytest.raises(errors.Unavailable):
        await ctl.transport(DEN, "pause")
    await ctl.close()

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest
from conftest import DEN, KITCHEN

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


class ChangedPortDriver(FakeDriver):
    """Discovery snapshots a port; connecting an old snapshot is refused."""

    def __init__(self):
        super().__init__()
        self.port = 1234
        self.attempts = []
        self.host_scopes = []
        self.recovery_results = None

    def _found(self, tv):
        return replace(super()._found(tv), handle=self.port)

    async def discover(self, timeout, hosts=None):
        self.host_scopes.append(hosts)
        found = await super().discover(timeout, hosts)
        return self.recovery_results if self.recovery_results is not None else found

    async def connect(self, tv):
        self.attempts.append((tv.key, tv.handle))
        if tv.handle != self.port:
            raise errors.Unreachable("Old service port refused the connection")
        return await super().connect(tv)


async def test_fresh_connection_rediscovers_changed_port_for_same_tv_only():
    driver = ChangedPortDriver()
    ctl = Controller([driver], tv_addresses=["192.0.2.10", "192.0.2.11"])
    driver.tvs["FAKE-2"].paired = True
    await ctl.scan()
    await ctl.power(KITCHEN)
    other_session = ctl._entries[KITCHEN].session
    other_found = ctl._entries[KITCHEN].found
    driver.port = 5678
    assert (await ctl.power(DEN))["power"] == "off"
    assert driver.attempts[-2:] == [("FAKE-1", 1234), ("FAKE-1", 5678)]
    assert driver.scans == 2
    assert driver.host_scopes == [["192.0.2.10", "192.0.2.11"]] * 2
    assert ctl._entries[KITCHEN].session is other_session
    assert ctl._entries[KITCHEN].found is other_found
    assert not other_session.closed
    await ctl.close()


@pytest.mark.parametrize("bad_match", ["foreign", "driver", "duplicate", "unpaired", "empty"])
async def test_rediscovery_never_retargets_by_name_or_address(bad_match):
    driver = ChangedPortDriver()
    ctl = Controller([driver])
    await ctl.scan()
    original = ctl._entries[DEN].found
    driver.port = 5678
    replacement = driver._found(driver.tvs["FAKE-1"])
    results = {
        "foreign": [replace(replacement, key="OTHER-HARDWARE")],
        "driver": [replace(replacement, driver="other-driver")],
        "duplicate": [replacement, replace(replacement, handle=9999)],
        "unpaired": [replace(replacement, paired=False)],
        "empty": [],
    }
    driver.recovery_results = results[bad_match]
    with pytest.raises(errors.Unreachable):
        await ctl.power(DEN)
    assert driver.attempts == [("FAKE-1", 1234)]
    assert ctl._entries[DEN].found is original
    assert not ctl._entries[DEN].reachable
    await ctl.close()


async def test_rediscovery_and_connection_retry_are_bounded():
    driver = ChangedPortDriver()
    ctl = Controller([driver])
    await ctl.scan()
    driver.port = 5678
    driver.recovery_results = [replace(driver._found(driver.tvs["FAKE-1"]), handle=9999)]
    with pytest.raises(errors.Unreachable):
        await ctl.power(DEN)
    assert driver.scans == 2
    assert driver.attempts == [("FAKE-1", 1234), ("FAKE-1", 9999)]
    await ctl.close()


async def test_new_session_action_failure_is_not_replayed():
    driver = ChangedPortDriver()
    ctl = Controller([driver])
    calls = []

    async def uncertain_action(session):
        calls.append(session)
        await session.volume_step(True)
        raise ConnectionError("Reply lost after applying the volume tap")

    with pytest.raises(errors.Unreachable):
        await ctl.call(DEN, None, uncertain_action)
    assert len(calls) == 1
    assert driver.tvs["FAKE-1"].volume == 35
    assert driver.scans == 1
    await ctl.close()


async def test_recovery_discovery_timeout_and_cancellation():
    driver = ChangedPortDriver()
    ctl = Controller([driver])
    await ctl.scan()
    driver.port = 5678

    async def failed_scan(_driver):
        raise TimeoutError("Discovery exceeded its bound")

    ctl._scan_one = failed_scan
    with pytest.raises(errors.Unreachable):
        await ctl.power(DEN)

    async def cancelled_scan(_driver):
        raise asyncio.CancelledError()

    ctl._scan_one = cancelled_scan
    with pytest.raises(asyncio.CancelledError):
        await ctl.power(DEN)
    assert len(driver.attempts) == 2
    await ctl.close()

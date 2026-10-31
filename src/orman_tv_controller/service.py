"""The controller: the TVs its drivers find, and one call at a time to each.

A TV's id is stable and opaque (`tv_` + a hash of driver and the driver's own
identifier), so a client can keep it. A scan is reused for `scan_ttl_s`; a TV a
later scan misses stays listed as unreachable. A connection to a TV is kept
for `keepalive_s` after its last call (a remote's volume taps shouldn't
reconnect each time), then closed; nothing polls in between.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import capabilities as caps
from . import errors
from .drivers.base import Driver, FoundTv, PairStep, TvSession


def tv_id(driver: str, key: str) -> str:
    return "tv_" + hashlib.sha256(f"{driver}:{key}".encode()).hexdigest()[:12]


@dataclass
class Entry:
    id: str
    found: FoundTv
    driver: Driver
    reachable: bool = True
    capabilities: set[str] | None = None  # None until a connection said
    session: TvSession | None = None
    closer: asyncio.TimerHandle | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def public(self) -> dict[str, Any]:
        f = self.found
        return {
            "id": self.id,
            "name": f.name,
            "driver": f.driver,
            "address": f.address,
            "model": f.model,
            "paired": f.paired,
            "to_pair": list(f.to_pair),
            "reachable": self.reachable,
            "capabilities": sorted(self.capabilities or ()),
        }


class Controller:
    def __init__(
        self,
        drivers: list[Driver],
        *,
        tv_addresses: list[str] | None = None,
        scan_timeout_s: float = 5,
        scan_ttl_s: float = 60,
        call_timeout_s: float = 10,
        keepalive_s: float = 20,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.drivers = drivers
        self.tv_addresses = tv_addresses or None
        self.scan_timeout_s = scan_timeout_s
        self.scan_ttl_s = scan_ttl_s
        self.call_timeout_s = call_timeout_s
        self.keepalive_s = keepalive_s
        self._now = now
        self._entries: dict[str, Entry] = {}
        self._scanned_at: float | None = None
        self._scan_lock = asyncio.Lock()

    # -- finding TVs ---------------------------------------------------------

    async def scan(self, *, force: bool = False) -> None:
        async with self._scan_lock:
            at = self._scanned_at
            fresh = at is not None and self._now() - at < self.scan_ttl_s
            if fresh and not force:
                return
            results = await asyncio.gather(
                *(self._scan_one(d) for d in self.drivers), return_exceptions=True
            )
            seen: set[str] = set()
            for driver, result in zip(self.drivers, results, strict=True):
                if isinstance(result, BaseException):
                    continue
                for found in result:
                    entry_id = tv_id(driver.name, found.key)
                    seen.add(entry_id)
                    entry = self._entries.get(entry_id)
                    if entry is None:
                        self._entries[entry_id] = Entry(entry_id, found, driver)
                    else:
                        entry.found, entry.reachable = found, True
            for entry_id, entry in self._entries.items():
                if entry_id not in seen:
                    entry.reachable = False
            # An empty scan is a hiccup, not a house without TVs: don't keep it.
            self._scanned_at = self._now() if seen else None

    async def _scan_one(self, driver: Driver) -> list[FoundTv]:
        async with asyncio.timeout(self.scan_timeout_s + 5):
            return await driver.discover(self.scan_timeout_s, self.tv_addresses)

    async def list_tvs(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        await self.scan(force=refresh)
        entries = sorted(self._entries.values(), key=lambda e: e.found.name.lower())
        # what each paired TV can do is learnt from a connection, once
        await asyncio.gather(
            *(
                self._learn(e)
                for e in entries
                if e.reachable and e.found.paired and (refresh or e.capabilities is None)
            ),
            return_exceptions=True,
        )
        return [e.public() for e in entries]

    async def _learn(self, entry: Entry) -> None:
        await self.call(entry.id, None, lambda s: _nothing())

    async def entry(self, entry_id: str) -> Entry:
        if entry_id not in self._entries:
            await self.scan()
        entry = self._entries.get(entry_id)
        if entry is None:
            raise errors.UnknownTv(f"No TV {entry_id!r} on this controller.")
        return entry

    async def describe(self, entry_id: str) -> dict[str, Any]:
        entry = await self.entry(entry_id)
        if entry.found.paired and entry.capabilities is None:
            try:
                await self._learn(entry)
            except errors.ControllerError:
                pass
        return entry.public()

    # -- one call to one TV ----------------------------------------------------

    async def call(
        self,
        entry_id: str,
        need: str | None,
        act: Callable[[TvSession], Awaitable[Any]],
    ) -> Any:
        """Run `act` on the TV's session: the TV must list `need`, and it must be
        usable now. A failed connection refreshes that TV's discovery details
        before one retry. An action on a newly opened session is never replayed."""
        entry = await self.entry(entry_id)
        if not entry.found.paired:
            raise errors.NotPaired(
                f"{entry.found.name} isn't paired with this controller; pair it from the"
                " app at home or on the controller's page."
            )
        async with entry.lock:
            for attempt in (1, 2):
                fresh = entry.session is None
                connected = False
                try:
                    async with asyncio.timeout(self.call_timeout_s):
                        session = await self._session(entry)
                        connected = True
                        if need is not None:
                            if need not in (entry.capabilities or ()):
                                raise errors.Unsupported(
                                    f"{entry.found.name} doesn't support {need}."
                                )
                            if not session.is_available(need):
                                if attempt == 1 and not fresh:
                                    # a kept connection can hold what the TV said while
                                    # asleep: ask a fresh one before saying no
                                    await self._drop(entry)
                                    continue
                                raise errors.Unavailable(
                                    f"{entry.found.name} can't do {need} right now."
                                )
                        result = await act(session)
                    self._keep(entry)
                    entry.reachable = True
                    return result
                except errors.ControllerError as err:
                    if isinstance(err, errors.Unreachable | errors.NotPaired):
                        await self._drop(entry)
                        if (
                            isinstance(err, errors.Unreachable)
                            and attempt == 1
                            and (not fresh or not connected)
                        ):
                            refreshed = await self._rediscover(entry)
                            if refreshed or not fresh:
                                continue
                        if isinstance(err, errors.Unreachable):
                            entry.reachable = False
                    else:
                        self._keep(entry)
                    raise
                except TimeoutError:
                    await self._drop(entry)
                    raise errors.TvTimeout(
                        f"{entry.found.name} didn't answer within {self.call_timeout_s:g} s."
                    ) from None
                except Exception as err:
                    await self._drop(entry)
                    if (
                        attempt == 1
                        and (not fresh or not connected)
                        and entry.driver.is_connection_error(err)
                    ):
                        refreshed = await self._rediscover(entry)
                        if refreshed or not fresh:
                            continue
                    known = entry.driver.translate(err, entry.found.name)
                    if known is None:
                        raise
                    if isinstance(known, errors.Unreachable):
                        entry.reachable = False
                    raise known from None
        raise AssertionError("unreachable")  # pragma: no cover

    async def _rediscover(self, entry: Entry) -> bool:
        """Refresh one stable hardware identity, without touching other sessions."""
        try:
            found = await self._scan_one(entry.driver)
        except Exception:
            return False
        matches = [
            tv for tv in found if tv.driver == entry.driver.name and tv.key == entry.found.key
        ]
        if len(matches) != 1 or not matches[0].paired:
            return False
        entry.found = matches[0]
        return True

    async def _session(self, entry: Entry) -> TvSession:
        if entry.closer is not None:
            entry.closer.cancel()
            entry.closer = None
        if entry.session is None:
            entry.session = await entry.driver.connect(entry.found)
            entry.capabilities = entry.session.capabilities()
        return entry.session

    def _keep(self, entry: Entry) -> None:
        if entry.session is None:
            return
        if self.keepalive_s <= 0:
            asyncio.get_running_loop().create_task(self._drop(entry))
            return
        if entry.closer is not None:
            entry.closer.cancel()
        entry.closer = asyncio.get_running_loop().call_later(
            self.keepalive_s, lambda: asyncio.ensure_future(self._idle_close(entry))
        )

    async def _idle_close(self, entry: Entry) -> None:
        async with entry.lock:
            await self._drop(entry)

    async def _drop(self, entry: Entry) -> None:
        if entry.closer is not None:
            entry.closer.cancel()
            entry.closer = None
        session, entry.session = entry.session, None
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        for entry in self._entries.values():
            await self._drop(entry)
        for driver in self.drivers:
            await driver.close()

    # -- the commands ------------------------------------------------------------

    async def power(self, entry_id: str) -> dict[str, Any]:
        state = await self.call(entry_id, caps.POWER, lambda s: s.power_state())
        return {"tv": entry_id, "power": state}

    async def set_power(self, entry_id: str, power: str) -> dict[str, Any]:
        if power not in ("on", "off"):
            raise errors.BadRequest('power is "on" or "off"')
        await self.call(entry_id, caps.POWER, lambda s: s.set_power(power == "on"))
        return {"tv": entry_id, "power": power}

    async def volume(self, entry_id: str) -> dict[str, Any]:
        level = await self.call(entry_id, caps.VOLUME_LEVEL, lambda s: s.volume())
        return {"tv": entry_id, "volume": _level(level)}

    async def change_volume(self, entry_id: str, change: str | None, level: Any) -> dict[str, Any]:
        if level is not None:
            try:
                target = max(0.0, min(100.0, float(level)))
            except (TypeError, ValueError):
                raise errors.BadRequest("level is a number, 0-100") from None
            await self.call(entry_id, caps.VOLUME_SET, lambda s: s.set_volume(target))
            return {"tv": entry_id, "volume": _level(target)}
        if change not in ("up", "down"):
            raise errors.BadRequest('send {"change": "up"|"down"} or {"level": 0-100}')

        async def step(s: TvSession) -> float | None:
            await s.volume_step(change == "up")
            if s.is_available(caps.VOLUME_LEVEL) and caps.VOLUME_LEVEL in s.capabilities():
                return await s.volume()
            return None

        level_now = await self.call(entry_id, caps.VOLUME_STEP, step)
        return {"tv": entry_id, "change": change, "volume": _level(level_now)}

    async def apps(self, entry_id: str) -> dict[str, Any]:
        found = await self.call(entry_id, caps.APPS_LIST, lambda s: s.apps())
        return {"tv": entry_id, "apps": [{"id": a.id, "name": a.name} for a in found]}

    async def launch(self, entry_id: str, app_id: str | None, name: str | None) -> dict[str, Any]:
        if app_id:
            await self.call(entry_id, caps.APPS_LAUNCH, lambda s: s.launch_app(app_id))
            return {"tv": entry_id, "app": {"id": app_id}}
        if not name or not name.strip():
            raise errors.BadRequest('send {"app_id": "<id from the app list>"} or {"name": "..."}')
        listed = (await self.apps(entry_id))["apps"]
        hit = match_app(listed, name)
        await self.call(entry_id, caps.APPS_LAUNCH, lambda s: s.launch_app(hit["id"]))
        return {"tv": entry_id, "app": hit}

    async def transport(self, entry_id: str, command: str) -> dict[str, Any]:
        if command not in caps.TRANSPORT_COMMANDS:
            raise errors.BadRequest(f"command is one of {', '.join(caps.TRANSPORT_COMMANDS)}")
        await self.call(entry_id, f"transport.{command}", lambda s: s.transport(command))
        return {"tv": entry_id, "command": command}

    async def remote_key(self, entry_id: str, key: str) -> dict[str, Any]:
        if key not in caps.REMOTE_KEYS_LIST:
            raise errors.BadRequest(f"key is one of {', '.join(caps.REMOTE_KEYS_LIST)}")
        await self.call(entry_id, caps.REMOTE_KEYS, lambda s: s.remote_key(key))
        return {"tv": entry_id, "key": key}

    # -- pairing -------------------------------------------------------------------

    async def pair_begin(self, entry_id: str) -> dict[str, Any]:
        entry = await self.entry(entry_id)
        async with entry.lock:
            await self._drop(entry)
            step = await entry.driver.pair_begin(entry.found)
        return self._pair_said(entry, step)

    async def pair_pin(self, entry_id: str, pin: str) -> dict[str, Any]:
        entry = await self.entry(entry_id)
        if not re.fullmatch(r"\d{4,8}", str(pin or "").strip()):
            raise errors.BadRequest("pin is the 4-digit code the TV shows")
        async with entry.lock:
            step = await entry.driver.pair_pin(entry.found, str(pin).strip())
        if step.done:
            entry.capabilities = None  # learnt on the next call
        return self._pair_said(entry, step)

    async def pair_cancel(self, entry_id: str) -> dict[str, Any]:
        entry = await self.entry(entry_id)
        await entry.driver.pair_cancel(entry.found)
        return {"tv": entry_id, "cancelled": True}

    async def unpair(self, entry_id: str) -> dict[str, Any]:
        """Forget this TV's pairing: it needs pairing again before control works."""
        entry = await self.entry(entry_id)
        async with entry.lock:
            await self._drop(entry)
            await entry.driver.pair_cancel(entry.found)
            await entry.driver.unpair(entry.found)
            entry.capabilities = None
        return {"tv": entry.id, "paired": entry.found.paired, "to_pair": list(entry.found.to_pair)}

    @staticmethod
    def _pair_said(entry: Entry, step: PairStep) -> dict[str, Any]:
        return {
            "tv": entry.id,
            "step": step.step or None,
            "needs_pin": step.needs_pin,
            "done": step.done,
            "remaining": step.remaining,
            "paired": entry.found.paired,
        }


async def _nothing() -> None:
    return None


def _level(level: Any) -> int | None:
    return None if level is None else int(round(float(level)))


def _fold(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower().replace("+", "plus"))


def match_app(apps: list[dict[str, str]], wanted: str) -> dict[str, str]:
    """By name, fuzzy but never a guess between two: exact, prefix, contains,
    then close spelling (from the tv plugin)."""
    want = _fold(wanted)
    if not want:
        raise errors.BadRequest("name is empty")
    for test in (
        lambda n: n == want,
        lambda n: n.startswith(want),
        lambda n: want in n,
    ):
        hits = [a for a in apps if test(_fold(a["name"]))]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = ", ".join(a["name"] for a in hits[:6])
            raise errors.BadRequest(f"Which app: {names}?", code="ambiguous_app")
    folded = {_fold(a["name"]): a for a in apps}
    close = difflib.get_close_matches(want, list(folded), n=1, cutoff=0.75)
    if close:
        return folded[close[0]]
    raise errors.NotFound(f"No app like {wanted!r} on this TV.", code="unknown_app")

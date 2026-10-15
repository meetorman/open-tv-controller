"""A pretend house of TVs: for tests, and for building a client without a TV
(`orman-tv-controller serve --fake`). Pairing PIN is always 1234."""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import capabilities as caps
from .. import errors
from .base import App, Driver, FoundTv, PairStep, TvSession

PIN = "1234"


@dataclass
class FakeTv:
    key: str
    name: str
    address: str
    caps: set[str]
    paired: bool = True
    power: str = "off"
    volume: float = 30.0
    apps: list[App] = field(
        default_factory=lambda: [
            App("com.example.movies", "Movies"),
            App("com.example.tube", "Tube"),
            App("org.videolan.vlc", "VLC"),
        ]
    )
    app: str | None = None
    playing: bool = False
    keys: list[str] = field(default_factory=list)
    reachable: bool = True


def house() -> list[FakeTv]:
    full = set(caps.ALL)
    return [
        FakeTv("FAKE-1", "Den", "192.0.2.10", full),
        # a TV whose volume lives on a receiver it can't reach
        FakeTv(
            "FAKE-2",
            "Kitchen",
            "192.0.2.11",
            full - {caps.VOLUME_LEVEL, caps.VOLUME_STEP, caps.VOLUME_SET},
            paired=False,
        ),
    ]


class FakeSession(TvSession):
    def __init__(self, tv: FakeTv, driver: FakeDriver) -> None:
        self.tv = tv
        self.driver = driver
        self.closed = False

    def _check(self) -> None:
        if not self.tv.reachable:
            raise ConnectionError(f"{self.tv.name} went away")

    def capabilities(self) -> set[str]:
        return set(self.tv.caps)

    def is_available(self, capability: str) -> bool:
        if capability in ("transport.pause", "transport.stop", "transport.next"):
            return self.tv.playing
        return capability in self.tv.caps

    async def power_state(self) -> str | None:
        self._check()
        return self.tv.power

    async def set_power(self, on: bool) -> None:
        self._check()
        self.tv.power = "on" if on else "off"

    async def volume(self) -> float | None:
        self._check()
        return self.tv.volume

    async def volume_step(self, up: bool) -> None:
        self._check()
        self.tv.volume = max(0.0, min(100.0, self.tv.volume + (5 if up else -5)))

    async def set_volume(self, level: float) -> None:
        self._check()
        self.tv.volume = level

    async def apps(self) -> list[App]:
        self._check()
        return list(self.tv.apps)

    async def launch_app(self, app_id: str) -> None:
        self._check()
        self.tv.app, self.tv.power = app_id, "on"

    async def transport(self, command: str) -> None:
        self._check()
        self.tv.playing = command in ("play", "play_pause") and not (
            command == "play_pause" and self.tv.playing
        )

    async def remote_key(self, key: str) -> None:
        self._check()
        self.tv.keys.append(key)

    async def close(self) -> None:
        self.closed = True
        self.driver.closes += 1


class FakeDriver(Driver):
    name = "fake"

    def __init__(self, tvs: list[FakeTv] | None = None) -> None:
        self.tvs = {t.key: t for t in (tvs if tvs is not None else house())}
        self.pairing: dict[str, list[str]] = {}
        self.connects = 0
        self.closes = 0
        self.scans = 0

    def _found(self, t: FakeTv) -> FoundTv:
        to_pair = [] if t.paired else ["airplay", "companion"]
        return FoundTv(
            driver=self.name,
            key=t.key,
            name=t.name,
            address=t.address,
            model="FakeTV",
            paired=t.paired,
            to_pair=to_pair,
            handle=t,
        )

    async def discover(self, timeout: float, hosts: list[str] | None = None) -> list[FoundTv]:
        self.scans += 1
        return [
            self._found(t)
            for t in self.tvs.values()
            if t.reachable and (not hosts or t.address in hosts)
        ]

    async def connect(self, tv: FoundTv) -> TvSession:
        fake = self.tvs[tv.key]
        if not fake.paired:
            raise errors.NotPaired(f"{tv.name} isn't paired with this controller.")
        if not fake.reachable:
            raise errors.Unreachable(f"Couldn't reach {tv.name}.")
        self.connects += 1
        return FakeSession(fake, self)

    async def pair_begin(self, tv: FoundTv) -> PairStep:
        fake = self.tvs[tv.key]
        if not fake.reachable:
            raise errors.Unreachable(f"Couldn't reach {tv.name}.")
        if fake.paired:
            return PairStep(step="", needs_pin=False, done=True)
        left = self.pairing.setdefault(tv.key, ["airplay", "companion"])
        return PairStep(step=left[0], needs_pin=True, done=False, remaining=list(left))

    async def pair_pin(self, tv: FoundTv, pin: str) -> PairStep:
        left = self.pairing.get(tv.key)
        if not left:
            raise errors.BadRequest("No pairing in progress for this TV; start one first.")
        if pin != PIN:
            self.pairing.pop(tv.key, None)
            raise errors.PairingFailed(f"{tv.name} didn't accept the PIN; start again.")
        left.pop(0)
        if not left:
            self.pairing.pop(tv.key, None)
            self.tvs[tv.key].paired = True
            tv.paired, tv.to_pair = True, []
            return PairStep(step="", needs_pin=False, done=True)
        tv.to_pair = list(left)
        return PairStep(step="", needs_pin=False, done=False, remaining=list(left))

    async def pair_cancel(self, tv: FoundTv) -> None:
        self.pairing.pop(tv.key, None)

    async def unpair(self, tv: FoundTv) -> None:
        self.tvs[tv.key].paired = False
        tv.paired, tv.to_pair = False, ["airplay", "companion"]

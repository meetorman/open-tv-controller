"""The pyatv driver against stand-ins for pyatv's objects (no TV, no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyatv import exceptions
from pyatv.const import FeatureName, FeatureState, PowerState

from orman_tv_controller import capabilities as caps
from orman_tv_controller import errors
from orman_tv_controller.drivers import pyatv_driver as d


class Features:
    def __init__(self, states):
        self.states = states

    def get_feature(self, name):
        return SimpleNamespace(state=self.states.get(name.name, FeatureState.Unsupported))


class Calls:
    def __init__(self):
        self.log = []

    def __getattr__(self, name):
        async def call(*args):
            self.log.append((name, *args))

        return call


def atv(states, **extra):
    a = SimpleNamespace(
        features=Features(states),
        power=Calls(),
        audio=Calls(),
        remote_control=Calls(),
        apps=Calls(),
    )
    a.power.power_state = PowerState.On
    a.audio.volume = 41.6
    for k, v in extra.items():
        setattr(a, k, v)
    return a


APPLE_TV_4K = {f.name: FeatureState.Available for f in FeatureName}


def test_capabilities_from_features():
    states = dict(APPLE_TV_4K)
    states["Volume"] = states["SetVolume"] = FeatureState.Unsupported
    states["Pause"] = FeatureState.Unavailable  # supported, not now
    s = d.PyatvSession(atv(states))
    got = s.capabilities()
    assert caps.POWER in got and caps.VOLUME_STEP in got and caps.REMOTE_KEYS in got
    assert caps.VOLUME_LEVEL not in got and caps.VOLUME_SET not in got
    assert "transport.pause" in got and not s.is_available("transport.pause")
    assert s.is_available("transport.play")


def test_nothing_supported():
    assert d.PyatvSession(atv({})).capabilities() == set()


async def test_session_calls():
    a = atv(APPLE_TV_4K)
    s = d.PyatvSession(a)
    assert await s.power_state() == "on"
    assert await s.volume() == 41.6
    await s.set_power(False)
    await s.volume_step(True)
    await s.set_volume(20)
    await s.transport("play_pause")
    await s.remote_key("home")
    await s.launch_app("com.example.app")
    assert ("turn_off",) in a.power.log
    assert ("volume_up",) in a.audio.log and ("set_volume", 20.0) in a.audio.log
    assert ("play_pause",) in a.remote_control.log and ("home",) in a.remote_control.log
    assert ("launch_app", "com.example.app") in a.apps.log


def test_apple_tv_filter():
    def cfg(model, raw="", os="Unknown"):
        return SimpleNamespace(
            device_info=SimpleNamespace(
                model=SimpleNamespace(name=model),
                raw_model=raw,
                operating_system=SimpleNamespace(name=os),
            )
        )

    # what a scan of a real home network said: only the tvOS ones are Apple TVs
    assert d.is_apple_tv(cfg("AppleTV4KGen3", "AppleTV14,1", "TvOS"))
    assert d.is_apple_tv(cfg("AppleTV4KGen2", "AppleTV11,1", "TvOS"))
    assert not d.is_apple_tv(cfg("AppleTV4KGen3", "Macmini9,1", "MacOS"))
    assert not d.is_apple_tv(cfg("AppleTV4KGen3", "AppleTV14,1", "MacOS"))
    assert not d.is_apple_tv(cfg("Unknown", "MacBookPro18,2", "MacOS"))
    assert not d.is_apple_tv(cfg("Unknown", "Arc Ultra"))
    assert not d.is_apple_tv(cfg("Unknown", "AirPort10,115", "AirPortOS"))
    assert not d.is_apple_tv(cfg("Gen4K", "Macmini9,1"))  # OS unknown: the raw model says Mac
    assert d.is_apple_tv(cfg("Gen3", "AppleTV3,2", "Legacy"))

    assert d.is_apple_tv(cfg("Gen4K"))
    assert d.is_apple_tv(cfg("Unknown", "AppleTV14,1"))
    assert not d.is_apple_tv(cfg("HomePod"))
    assert d.model_of(cfg("Unknown", "AppleTV14,1")) == "AppleTV14,1"


@pytest.mark.parametrize(
    "exc, kind",
    [
        (exceptions.NotSupportedError("x"), errors.Unsupported),
        (exceptions.AuthenticationError("x"), errors.NotPaired),
        (exceptions.ConnectionFailedError("x"), errors.Unreachable),
        (OSError("no route"), errors.Unreachable),
    ],
)
def test_translate(exc, kind):
    assert isinstance(d.translate(exc, "Den"), kind)


def test_translate_unknown():
    assert d.translate(ValueError("x"), "Den") is None


async def test_unpair_forgets_the_credentials(tmp_path):
    from pyatv import conf
    from pyatv.const import PairingRequirement, Protocol

    from orman_tv_controller.drivers.base import FoundTv

    config = conf.AppleTV("192.0.2.20", "Den")
    for protocol, port in ((Protocol.AirPlay, 7000), (Protocol.Companion, 49153)):
        config.add_service(
            conf.ManualService(
                "id-1",
                protocol,
                port,
                {},
                credentials="secret",
                pairing_requirement=PairingRequirement.Mandatory,
            )
        )
    driver = d.PyatvDriver(str(tmp_path / "pyatv.conf"))
    storage = await driver.storage()
    await storage.update_settings(config)
    await storage.save()
    assert await driver._to_pair(config) == []
    tv = FoundTv("pyatv", "id-1", "Den", "192.0.2.20", paired=True, handle=config)
    await driver.unpair(tv)
    assert not tv.paired and tv.to_pair == ["airplay", "companion"]
    assert config.get_service(Protocol.AirPlay).credentials is None
    again = d.PyatvDriver(str(tmp_path / "pyatv.conf"))  # what a restart reads
    assert await again._to_pair(config) == ["airplay", "companion"]

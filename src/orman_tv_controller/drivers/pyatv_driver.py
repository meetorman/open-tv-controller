"""Apple TVs through pyatv.

Seeded from Orman's `tv` plugin: the Apple TV filter, the scan/connect with
pyatv's file storage, the feature checks and the close. pyatv's asyncio API is
called in the server's own loop (never `atvremote`).

Credentials live in pyatv's own file storage in the controller's data folder;
pairing writes to it. A tvOS Apple TV needs two pairings, AirPlay and
Companion, each with a PIN the TV shows.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from .. import capabilities as caps
from .. import errors
from .base import App, Driver, FoundTv, PairStep, TvSession

# Apple TV hardware as the scan names it; a model this pyatv doesn't know yet
# still reports a raw model string starting "AppleTV".
_APPLE_TV_MODELS = {
    "AppleTVGen1",
    "Gen2",
    "Gen3",
    "Gen4",
    "Gen4K",
    "AppleTV4KGen2",
    "AppleTV4KGen3",
}

# The pairings a TV needs before control works, in the order they're done.
_PAIR_ORDER = ("airplay", "companion")
_PROTOCOL = {"airplay": "AirPlay", "companion": "Companion"}

_TRANSPORT_FEATURES = {
    "play": "Play",
    "pause": "Pause",
    "play_pause": "PlayPause",
    "stop": "Stop",
    "next": "Next",
    "previous": "Previous",
}
_KEY_FEATURES = {
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "select": "Select",
    "menu": "Menu",
    "home": "Home",
}

# capability → the pyatv features it needs (all of them)
FEATURES: dict[str, tuple[str, ...]] = {
    caps.POWER: ("PowerState", "TurnOn", "TurnOff"),
    caps.VOLUME_LEVEL: ("Volume",),
    caps.VOLUME_STEP: ("VolumeUp", "VolumeDown"),
    caps.VOLUME_SET: ("SetVolume",),
    caps.APPS_LIST: ("AppList",),
    caps.APPS_LAUNCH: ("LaunchApp",),
    **{f"transport.{c}": (f,) for c, f in _TRANSPORT_FEATURES.items()},
    caps.REMOTE_KEYS: tuple(_KEY_FEATURES.values()),
}


def is_apple_tv(config: Any) -> bool:
    """An Apple TV, not a Mac, HomePod or another AirPlay receiver. The
    operating system decides when pyatv knows it (a Mac can announce an Apple
    TV model); otherwise the model does."""
    info = getattr(config, "device_info", None)
    if info is None:
        return False
    os_name = getattr(getattr(info, "operating_system", None), "name", "Unknown")
    if os_name == "TvOS":
        return True
    if os_name not in ("Unknown", "Legacy"):  # MacOS, AirPortOS
        return False
    raw = str(getattr(info, "raw_model", "") or "")
    if raw.startswith(("Mac", "iMac", "MacBook", "AudioAccessory", "AirPort")):
        return False
    model = getattr(getattr(info, "model", None), "name", "")
    if model in _APPLE_TV_MODELS:
        return True
    return str(getattr(info, "raw_model", "") or "").startswith("AppleTV")


def model_of(config: Any) -> str | None:
    info = getattr(config, "device_info", None)
    raw = getattr(info, "raw_model", None)
    model = getattr(getattr(info, "model", None), "name", None)
    return model if model and model != "Unknown" else raw


def is_pyatv_error(err: BaseException, *names: str) -> bool:
    return type(err).__module__.startswith("pyatv") and any(
        cls.__name__ in names for cls in type(err).__mro__
    )


def translate(err: BaseException, what: str) -> errors.ControllerError | None:
    """A pyatv failure as a client error, or None when it isn't one we know."""
    if is_pyatv_error(err, "NotSupportedError"):
        return errors.Unsupported(f"{what} doesn't support that.")
    if is_pyatv_error(err, "AuthenticationError", "NoCredentialsError"):
        return errors.NotPaired(f"{what} isn't paired with this controller.")
    if is_pyatv_error(err, "ConnectionFailedError", "ConnectionLostError", "NoServiceError"):
        return errors.Unreachable(f"Couldn't reach {what}: {err}")
    if is_pyatv_error(err, "ProtocolError"):
        # Companion wraps a missing command reply in ProtocolError. The action
        # may already have applied, so report it without replaying the command.
        if isinstance(err.__cause__, TimeoutError):
            return errors.TvTimeout(
                f"{what} didn't confirm that action. Refresh its state before trying again."
            )
        return errors.Unavailable(f"{what} couldn't confirm that action: {err}")
    if is_pyatv_error(err, "BlockedStateError", "CommandError"):
        return errors.Unavailable(f"{what} can't do that right now: {err}")
    if isinstance(err, OSError):
        return errors.Unreachable(f"Couldn't reach {what}: {err}")
    return None


async def close_atv(atv: Any) -> None:
    try:
        pending = atv.close()
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=3)
    except Exception:  # noqa: BLE001 — closing never turns an answer into a failure
        pass


class PyatvSession(TvSession):
    def __init__(self, atv: Any) -> None:
        self.atv = atv

    def _state(self, feature: str) -> Any:
        from pyatv.const import FeatureName, FeatureState

        try:
            return self.atv.features.get_feature(getattr(FeatureName, feature)).state
        except Exception:  # noqa: BLE001 — a feature table that can't answer says no
            return FeatureState.Unsupported

    def capabilities(self) -> set[str]:
        from pyatv.const import FeatureState

        return {
            cap
            for cap, needs in FEATURES.items()
            if all(self._state(f) != FeatureState.Unsupported for f in needs)
        }

    def is_available(self, capability: str) -> bool:
        from pyatv.const import FeatureState

        ok = (FeatureState.Available, FeatureState.Unknown)
        return all(self._state(f) in ok for f in FEATURES.get(capability, ()))

    async def power_state(self) -> str | None:
        state = getattr(self.atv.power, "power_state", None)
        name = str(getattr(state, "name", "")).lower()
        return name if name in ("on", "off") else None

    async def set_power(self, on: bool) -> None:
        await (self.atv.power.turn_on() if on else self.atv.power.turn_off())

    async def volume(self) -> float | None:
        level = self.atv.audio.volume
        return None if level is None else float(level)

    async def volume_step(self, up: bool) -> None:
        await (self.atv.audio.volume_up() if up else self.atv.audio.volume_down())

    async def set_volume(self, level: float) -> None:
        await self.atv.audio.set_volume(float(level))

    async def apps(self) -> list[App]:
        found = await self.atv.apps.app_list()
        out = [
            App(id=str(a.identifier), name=str(a.name or a.identifier).replace("\xa0", " "))
            for a in found
        ]
        return sorted(out, key=lambda a: a.name.lower())

    async def launch_app(self, app_id: str) -> None:
        await self.atv.apps.launch_app(app_id)

    async def transport(self, command: str) -> None:
        await getattr(self.atv.remote_control, command)()

    async def remote_key(self, key: str) -> None:
        await getattr(self.atv.remote_control, key)()

    async def close(self) -> None:
        await close_atv(self.atv)


class PyatvDriver(Driver):
    name = "pyatv"

    def __init__(self, credentials_path: str, *, client_name: str = "Orman TV controller") -> None:
        self.credentials_path = os.path.expanduser(credentials_path)
        self.client_name = client_name
        self._storage: Any = None
        self._pairing: dict[str, tuple[Any, str]] = {}  # tv key → (handler, protocol)

    async def storage(self) -> Any:
        if self._storage is None:
            from pyatv.storage.file_storage import FileStorage

            os.makedirs(os.path.dirname(self.credentials_path) or ".", exist_ok=True)
            self._storage = FileStorage(self.credentials_path, asyncio.get_running_loop())
            await self._storage.load()
        return self._storage

    async def _to_pair(self, config: Any) -> list[str]:
        """The pairings this TV still needs, from the stored credentials."""
        from pyatv.const import PairingRequirement, Protocol

        settings = await (await self.storage()).get_settings(config)
        need = []
        for name in _PAIR_ORDER:
            service = config.get_service(getattr(Protocol, _PROTOCOL[name]))
            if service is None:
                continue
            if service.pairing not in (PairingRequirement.Mandatory, PairingRequirement.Optional):
                continue
            if not getattr(getattr(settings.protocols, name), "credentials", None):
                need.append(name)
        return need

    async def discover(self, timeout: float, hosts: list[str] | None = None) -> list[FoundTv]:
        import pyatv

        found = await pyatv.scan(
            asyncio.get_running_loop(),
            timeout=max(1, round(timeout)),
            hosts=hosts or None,
            storage=await self.storage(),
        )
        tvs = []
        for config in found:
            if not is_apple_tv(config):
                continue
            to_pair = await self._to_pair(config)
            tvs.append(
                FoundTv(
                    driver=self.name,
                    key=str(config.identifier),
                    name=str(config.name),
                    address=str(config.address),
                    model=model_of(config),
                    paired=not to_pair,
                    to_pair=to_pair,
                    handle=config,
                )
            )
        return sorted(tvs, key=lambda t: t.name.lower())

    async def connect(self, tv: FoundTv) -> TvSession:
        import pyatv

        if not tv.paired:
            raise errors.NotPaired(f"{tv.name} isn't paired with this controller; pair it first.")
        try:
            atv = await pyatv.connect(
                tv.handle, asyncio.get_running_loop(), storage=await self.storage()
            )
        except Exception as err:
            raise (translate(err, tv.name) or err) from None
        return PyatvSession(atv)

    def translate(self, err: BaseException, what: str) -> errors.ControllerError | None:
        return translate(err, what)

    def is_connection_error(self, err: BaseException) -> bool:
        return isinstance(err, OSError) or is_pyatv_error(
            err, "ConnectionFailedError", "ConnectionLostError"
        )

    async def pair_begin(self, tv: FoundTv) -> PairStep:
        import pyatv
        from pyatv.const import Protocol

        await self.pair_cancel(tv)
        to_pair = await self._to_pair(tv.handle)
        if not to_pair:
            return PairStep(step="", needs_pin=False, done=True, remaining=[])
        protocol = to_pair[0]
        try:
            handler = await pyatv.pair(
                tv.handle,
                getattr(Protocol, _PROTOCOL[protocol]),
                asyncio.get_running_loop(),
                storage=await self.storage(),
                name=self.client_name,
            )
            await handler.begin()
        except Exception as err:
            known = translate(err, tv.name)
            if isinstance(known, errors.Unreachable):
                raise known from None  # asleep or off the network: wake it and try again
            raise errors.PairingFailed(f"{tv.name} wouldn't start pairing: {err}") from None
        self._pairing[tv.key] = (handler, protocol)
        if not handler.device_provides_pin:
            # a pairing without a PIN finishes at once
            return await self._finish(tv, handler, protocol, None)
        return PairStep(step=protocol, needs_pin=True, done=False, remaining=to_pair)

    async def pair_pin(self, tv: FoundTv, pin: str) -> PairStep:
        held = self._pairing.get(tv.key)
        if held is None:
            raise errors.BadRequest("No pairing in progress for this TV; start one first.")
        handler, protocol = held
        return await self._finish(tv, handler, protocol, pin)

    async def _finish(self, tv: FoundTv, handler: Any, protocol: str, pin: str | None) -> PairStep:
        why = ""
        try:
            if pin is not None:
                handler.pin(pin)
            await handler.finish()
            ok = handler.has_paired
        except Exception as err:  # noqa: BLE001 — any failure is a failed pairing
            ok, why = False, str(err) or type(err).__name__
        finally:
            self._pairing.pop(tv.key, None)
            await _close_pairing(handler)
        if not ok:
            raise errors.PairingFailed(
                f"{tv.name} didn't accept the PIN for {protocol}"
                + (f" ({why})" if why else "")
                + "; start again for a new PIN."
            )
        await (await self.storage()).save()
        to_pair = await self._to_pair(tv.handle)
        tv.to_pair, tv.paired = to_pair, not to_pair
        return PairStep(step="", needs_pin=False, done=not to_pair, remaining=to_pair)

    async def pair_cancel(self, tv: FoundTv) -> None:
        held = self._pairing.pop(tv.key, None)
        if held is not None:
            await _close_pairing(held[0])

    async def unpair(self, tv: FoundTv) -> None:
        from pyatv.const import Protocol

        storage = await self.storage()
        settings = await storage.get_settings(tv.handle)
        for name in _PAIR_ORDER:
            getattr(settings.protocols, name).credentials = None
            service = tv.handle.get_service(getattr(Protocol, _PROTOCOL[name]))
            if service is not None:
                service.credentials = None  # the scan put them on the config too
        await storage.save()
        to_pair = await self._to_pair(tv.handle)
        tv.to_pair, tv.paired = to_pair, not to_pair

    async def close(self) -> None:
        for key in list(self._pairing):
            handler, _ = self._pairing.pop(key)
            await _close_pairing(handler)


async def _close_pairing(handler: Any) -> None:
    try:
        await asyncio.wait_for(handler.close(), timeout=3)
    except Exception:  # noqa: BLE001
        pass


async def import_credentials(source_path: str, dest_path: str, hosts: list[str]) -> list[str]:
    """Copy the credentials another pyatv file holds for these TVs into ours
    (a box that already paired its TVs, e.g. with `atvremote`)."""
    import pyatv
    from pyatv.storage.file_storage import FileStorage

    loop = asyncio.get_running_loop()
    source = FileStorage(os.path.expanduser(source_path), loop)
    await source.load()
    dest_path = os.path.expanduser(dest_path)
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    dest = FileStorage(dest_path, loop)
    await dest.load()
    copied = []
    for config in await pyatv.scan(loop, timeout=5, hosts=hosts, storage=source):
        if is_apple_tv(config):
            await dest.update_settings(config)
            copied.append(f"{config.name} ({config.address})")
    await dest.save()
    return copied

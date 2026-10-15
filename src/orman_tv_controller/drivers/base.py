"""The driver interface: what the controller needs from a kind of TV.

A driver finds its TVs, pairs them, and opens a session to one TV. The session
says what the TV can do (capability names, see `capabilities`) and does it.
pyatv (Apple TV) is the first driver; Roku or Google TV would be others.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..errors import ControllerError, Unreachable


@dataclass
class FoundTv:
    """One TV a driver found. `key` is the driver's own stable identifier."""

    driver: str
    key: str
    name: str
    address: str
    model: str | None = None
    paired: bool = False
    # what still needs pairing, in order (for pyatv: protocols)
    to_pair: list[str] = field(default_factory=list)
    handle: Any = None  # the driver's own object (a pyatv config)


@dataclass
class App:
    id: str
    name: str


@dataclass
class PairStep:
    """Where a pairing is. `needs_pin`: the TV shows a PIN to type back."""

    step: str  # the part being paired now (pyatv: a protocol name), or "" when done
    needs_pin: bool
    done: bool
    remaining: list[str] = field(default_factory=list)


class TvSession(ABC):
    """One open connection to one TV."""

    @abstractmethod
    def capabilities(self) -> set[str]: ...

    @abstractmethod
    def is_available(self, capability: str) -> bool:
        """Supported and usable right now (or not known: try it)."""

    @abstractmethod
    async def power_state(self) -> str | None: ...

    @abstractmethod
    async def set_power(self, on: bool) -> None: ...

    @abstractmethod
    async def volume(self) -> float | None: ...

    @abstractmethod
    async def volume_step(self, up: bool) -> None: ...

    @abstractmethod
    async def set_volume(self, level: float) -> None: ...

    @abstractmethod
    async def apps(self) -> list[App]: ...

    @abstractmethod
    async def launch_app(self, app_id: str) -> None: ...

    @abstractmethod
    async def transport(self, command: str) -> None: ...

    @abstractmethod
    async def remote_key(self, key: str) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class Driver(ABC):
    name: str = ""

    @abstractmethod
    async def discover(self, timeout: float, hosts: list[str] | None = None) -> list[FoundTv]: ...

    @abstractmethod
    async def connect(self, tv: FoundTv) -> TvSession: ...

    @abstractmethod
    async def pair_begin(self, tv: FoundTv) -> PairStep: ...

    @abstractmethod
    async def pair_pin(self, tv: FoundTv, pin: str) -> PairStep: ...

    @abstractmethod
    async def pair_cancel(self, tv: FoundTv) -> None: ...

    @abstractmethod
    async def unpair(self, tv: FoundTv) -> None:
        """Forget what pairing stored for this TV; set `tv.paired` and `tv.to_pair`."""

    def translate(self, err: BaseException, what: str) -> ControllerError | None:
        """A driver failure as a client error; None leaves it a 500."""
        if isinstance(err, OSError):
            return Unreachable(f"Couldn't reach {what}: {err}")
        return None

    def is_connection_error(self, err: BaseException) -> bool:
        """A lost or refused connection: drop the session and reconnect."""
        return isinstance(err, OSError)

    async def close(self) -> None:  # noqa: B027 — optional hook
        pass

"""What the controller keeps in its data folder.

    controller.json   id, name, the registration code, who may use it (`access`)
    tokens.json       bearer tokens, hashed
    pyatv.conf        Apple TV credentials (pyatv's own file storage)

The data folder is `$ORMAN_TVCTL_HOME`, else `~/.local/share/orman-tv-controller`
(the container uses /data).
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

# Crockford base32 minus I, L, O, U: easy to read off a page and type on a phone.
_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
REGISTRATION_TTL_S = 15 * 60

# Who may use the controller from the home network. `open` (the default): anyone
# on it, with no token, like a TV remote app. `members`: a token, as away.
ACCESS_OPEN = "open"
ACCESS_MEMBERS = "members"
ACCESS_MODES = (ACCESS_OPEN, ACCESS_MEMBERS)


def data_dir(explicit: str | None = None) -> Path:
    raw = explicit or os.environ.get("ORMAN_TVCTL_HOME") or "~/.local/share/orman-tv-controller"
    path = Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_private(path: Path, data: dict) -> None:
    """Write JSON readable by the owner only, atomically."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def new_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


@dataclass
class Registration:
    """The controller's registration with a TV control provider (README
    "Registration and the relay").

    The code is always there, renewed every 15 minutes and after each use: a
    member types it into the app to add this controller to their account (the
    first one registers it; more members can add it the same way). Once
    registered, `credential` opens the controller's connection to the provider
    and `key` checks the tokens the provider mints for it."""

    code: str
    expires_at: float
    provider: str | None = None  # the provider it is registered with (its base URL)
    credential: str | None = None
    key: str | None = None
    registered_at: float | None = None
    chosen_provider: str | None = None  # learned from the app that found it (or the admin API)

    @property
    def state(self) -> str:
        return "registered" if self.credential and self.key else "unregistered"

    def public(self) -> dict:
        return {
            "state": self.state,
            "code": self.code,
            "expires_at": int(self.expires_at),
            "provider": self.provider,
            "registered_at": int(self.registered_at) if self.registered_at else None,
        }


class ControllerState:
    def __init__(self, folder: Path, *, name: str | None = None) -> None:
        self.folder = folder
        self.path = folder / "controller.json"
        raw = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.id: str = raw.get("id") or "ctl_" + secrets.token_hex(8)
        self.name: str = name or raw.get("name") or default_name()
        access = raw.get("access")
        self.access: str = access if access in ACCESS_MODES else ACCESS_OPEN
        reg = raw.get("registration") or {}
        self.registration = Registration(
            code=reg.get("code") or new_code(),
            expires_at=float(reg.get("expires_at") or time.time() + REGISTRATION_TTL_S),
            provider=reg.get("provider") if reg.get("credential") else None,
            credential=reg.get("credential"),
            key=reg.get("key"),
            registered_at=reg.get("registered_at"),
            chosen_provider=reg.get("chosen_provider"),
        )
        # called after the registration state changes (Bonjour's TXT reg= follows it)
        self.on_registration_change: list[Callable[[], None]] = []
        self.save()

    def _registration_changed(self) -> None:
        for fn in list(self.on_registration_change):
            fn()

    def save(self) -> None:
        write_private(
            self.path,
            {
                "id": self.id,
                "name": self.name,
                "access": self.access,
                "registration": asdict(self.registration),
            },
        )

    def set_access(self, access: str) -> None:
        if access not in ACCESS_MODES:
            raise ValueError(f"access is {' or '.join(ACCESS_MODES)}; got {access!r}")
        self.access = access
        self.save()

    def current_registration(self, *, now: float | None = None) -> Registration:
        """The registration, its code renewed when it has expired."""
        now = time.time() if now is None else now
        if self.registration.expires_at <= now:
            self.new_registration_code(now=now)
        return self.registration

    def new_registration_code(self, *, now: float | None = None) -> Registration:
        now = time.time() if now is None else now
        self.registration.code = new_code()
        self.registration.expires_at = now + REGISTRATION_TTL_S
        self.save()
        return self.registration

    def registered(self, provider: str, credential: str, key: str) -> None:
        reg = self.registration
        reg.provider, reg.credential, reg.key, reg.registered_at = (
            provider,
            credential,
            key,
            time.time(),
        )
        self.new_registration_code()
        self._registration_changed()

    def unregistered(self) -> None:
        reg = self.registration
        was = reg.state
        reg.provider = reg.credential = reg.key = reg.registered_at = None
        reg.chosen_provider = None  # back to silence until an app names a provider again
        self.new_registration_code()
        if was != reg.state:
            self._registration_changed()


def default_name() -> str:
    host = socket.gethostname().split(".")[0] or "controller"
    return f"TV controller ({host})"

"""Bearer tokens.

Every API call carries `Authorization: Bearer <token>`. The controller asks its
verifiers in turn; the first that knows the token says what it may do (a
`Grant`). Phase 1 has one verifier, `LocalTokens`: tokens minted on the
controller itself (CLI or page), kept hashed in tokens.json.

`ProviderTokens` checks the tokens the TV control provider mints for a
member (`otp1.<claims>.<mac>`, README "Registration and the relay"): an
HMAC-SHA256 with the key the provider gave at registration, this controller's
id as audience, the provider as issuer, a few minutes long. The same token
works on the LAN and through the provider's relay. The routes only ever see a
`Grant`.

Scopes: `control` drives TVs (read state, power, volume, apps, transport,
keys). `pair` also pairs and unpairs TVs, but only on a request straight from
the home network (api.py checks that, as `/discovery` does); every token the
provider mints for a member carries it, so a member pairs from the app at
home and never through the relay. `admin` also mints and revokes tokens and
shows the registration code; it includes `control` and `pair` (anywhere: it
is the controller's own page).

Home-network access (`access` in controller.json, `open` by default): a
request straight from the home network (lan.py, the `/discovery` check: a
private, link-local or loopback source and no proxy header, so never the
relay) needs no token at all and gets `control` and `pair`, the way a TV
remote app works. `members` turns that off: the home network then needs a
token too, as away. Away from home (the relay) a token is always needed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .state import ControllerState, write_private

CONTROL = "control"
PAIR = "pair"
ADMIN = "admin"
SCOPES = (CONTROL, PAIR, ADMIN)
PREFIX = "otc_"


@dataclass(frozen=True)
class Grant:
    token_id: str
    name: str
    scopes: frozenset[str]
    issuer: str  # "local", or the provider's URL (phase 2)
    expires_at: float | None = None

    def allows(self, scope: str) -> bool:
        return scope in self.scopes or ADMIN in self.scopes


HOME_ISSUER = "home"


def home_grant(token_grant: Grant | None = None) -> Grant:
    """What an open controller allows a request straight from the home network:
    `control` and `pair`, plus whatever a token it carried allows."""
    if token_grant is not None:
        return Grant(
            token_id=token_grant.token_id,
            name=token_grant.name,
            scopes=token_grant.scopes | {CONTROL, PAIR},
            issuer=token_grant.issuer,
            expires_at=token_grant.expires_at,
        )
    return Grant(
        token_id="", name="home network", scopes=frozenset((CONTROL, PAIR)), issuer=HOME_ISSUER
    )


class Verifier(Protocol):
    async def verify(self, token: str) -> Grant | None:
        """The grant for this token, or None when this verifier doesn't know it."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LocalTokens:
    """Tokens minted on this controller. Only their hashes are kept."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rows: dict[str, dict] = {}
        self._stamp: tuple[int, int] | None = None
        self._load()

    def _load(self) -> None:
        """Read the file again when it changed (the CLI mints while the server runs)."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._rows, self._stamp = {}, None
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            rows = json.loads(self.path.read_text()).get("tokens", [])
            self._rows, self._stamp = {r["id"]: r for r in rows}, stamp

    def _save(self) -> None:
        write_private(self.path, {"tokens": list(self._rows.values())})
        st = self.path.stat()
        self._stamp = (st.st_mtime_ns, st.st_size)

    def mint(self, name: str, scopes: list[str], ttl_s: float | None = None) -> tuple[str, dict]:
        bad = [s for s in scopes if s not in SCOPES]
        if bad or not scopes:
            raise ValueError(f"scopes are {', '.join(SCOPES)}; got {scopes!r}")
        self._load()
        token_id = secrets.token_hex(4)
        while token_id in self._rows:
            token_id = secrets.token_hex(4)
        token = f"{PREFIX}{token_id}.{secrets.token_urlsafe(32)}"
        row = {
            "id": token_id,
            "name": name,
            "scopes": sorted(set(scopes)),
            "hash": _hash(token),
            "created_at": int(time.time()),
            "expires_at": int(time.time() + ttl_s) if ttl_s else None,
        }
        self._rows[token_id] = row
        self._save()
        return token, public(row)

    def list(self) -> list[dict]:
        self._load()
        return [public(r) for r in sorted(self._rows.values(), key=lambda r: r["created_at"])]

    def revoke(self, token_id: str) -> bool:
        self._load()
        if self._rows.pop(token_id, None) is None:
            return False
        self._save()
        return True

    def has_admin(self) -> bool:
        self._load()
        now = time.time()
        return any(
            ADMIN in r["scopes"] and (not r["expires_at"] or r["expires_at"] > now)
            for r in self._rows.values()
        )

    async def verify(self, token: str) -> Grant | None:
        if not token.startswith(PREFIX) or "." not in token:
            return None
        token_id = token[len(PREFIX) :].split(".", 1)[0]
        self._load()
        row = self._rows.get(token_id)
        if row is None or not hmac.compare_digest(row["hash"], _hash(token)):
            return None
        if row["expires_at"] and row["expires_at"] <= time.time():
            return None
        return Grant(
            token_id=row["id"],
            name=row["name"],
            scopes=frozenset(row["scopes"]),
            issuer="local",
            expires_at=row["expires_at"],
        )


def public(row: dict) -> dict:
    return {k: row[k] for k in ("id", "name", "scopes", "created_at", "expires_at")}


class Authenticator:
    def __init__(self, verifiers: list[Verifier]) -> None:
        self.verifiers = verifiers

    async def grant(self, header: str | None) -> Grant | None:
        if not header or not header.lower().startswith("bearer "):
            return None
        token = header[7:].strip()
        if not token:
            return None
        for verifier in self.verifiers:
            grant = await verifier.verify(token)
            if grant is not None:
                return grant
        return None


OTP_PREFIX = "otp1"


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check_provider_token(
    token: str, key: str, *, audience: str, issuer: str, now: float | None = None
) -> dict | None:
    """The claims of a provider-minted token, or None when it isn't one this
    key signed for this controller and provider, or it has expired."""
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != OTP_PREFIX:
        return None
    try:
        claims = json.loads(_unb64(parts[1]))
        mac = _unb64(parts[2])
        want = hmac.new(_unb64(key), f"{OTP_PREFIX}.{parts[1]}".encode(), hashlib.sha256).digest()
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict) or not hmac.compare_digest(want, mac):
        return None
    now = time.time() if now is None else now
    exp = claims.get("exp")
    if claims.get("aud") != audience or claims.get("iss") != issuer:
        return None
    if not isinstance(exp, int | float) or exp <= now:
        return None
    return claims


class ProviderTokens:
    """Tokens minted by the provider this controller is registered with."""

    def __init__(self, state: ControllerState) -> None:
        self.state = state

    async def verify(self, token: str) -> Grant | None:
        reg = self.state.registration
        if not token.startswith(OTP_PREFIX + ".") or not (reg.key and reg.provider):
            return None
        claims = check_provider_token(token, reg.key, audience=self.state.id, issuer=reg.provider)
        if claims is None:
            return None
        if CONTROL not in (claims.get("scope") or []):
            return None
        # a member who holds the controller may pair its TVs, from home only
        scopes = frozenset((CONTROL, PAIR))
        return Grant(
            token_id=str(claims.get("jti") or ""),
            name=str(claims.get("sub") or "member"),
            scopes=scopes,
            issuer=reg.provider,
            expires_at=float(claims["exp"]),
        )

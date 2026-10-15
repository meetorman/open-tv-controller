"""The controller's one outbound connection to its TV control provider.

It is a WebSocket to `<provider>/connect` (README "Registration and the
relay"), so nothing has to open at home. There is no built-in provider: a fresh
controller dials nobody. It learns its provider from the app that found it on
the home network (`POST /discovery/provider`), or from its operator
(`--provider`); once registered it keeps the provider it registered with, and
leaving it goes back to silence. While it has a provider:

- unregistered, it says hello with the code the page shows, and hears
  `registered` (with its credential and token key) when a member approves the
  code in the app;
- registered, it opens with its credential, and the provider passes down the
  calls a member makes away from home (`request`). Each is run against this
  controller's own HTTP API on the loopback, with the member's token, so a
  relayed call is checked exactly like a direct one;
- `unregistered` means nobody has it any more.

When the connection drops it comes back after a pause that grows to a minute.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from . import __version__
from .state import ControllerState

log = logging.getLogger(__name__)

RELAY_VIA = "1.1 orman-tv-relay"

TvsFn = Callable[[], Awaitable[list[dict[str, Any]]]]


def connect_url(provider: str) -> str:
    base = provider.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return base + "/connect"


class ProviderLink:
    def __init__(
        self,
        state: ControllerState,
        provider: str | None,
        local_base: str,
        tvs: TvsFn,
        *,
        heartbeat_s: float = 30,
    ) -> None:
        self.state = state
        self.pinned = provider  # the operator's --provider; None: learn it from the app
        self.local_base = local_base.rstrip("/")
        self.tvs = tvs
        self.heartbeat_s = heartbeat_s
        self.status = "off"  # off | connecting | connected
        self.error: str | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None
        self._code_timer: asyncio.TimerHandle | None = None
        self._session: aiohttp.ClientSession | None = None
        self._wake = asyncio.Event()
        self._welcome = asyncio.Event()

    @property
    def provider(self) -> str | None:
        """Where the link goes: the provider it registered with, else the operator's,
        else the one an app on the home network named; None: no outbound connection."""
        reg = self.state.registration
        return reg.provider or self.pinned or reg.chosen_provider

    def learn(self, provider: str) -> None:
        """An app that found this controller on the home network names its provider."""
        reg = self.state.registration
        if reg.chosen_provider != provider:
            reg.chosen_provider = provider
            self.state.save()
            self.reconnect()

    async def wait_connected(self, timeout: float) -> bool:
        """True once the provider has welcomed this connection (within `timeout`)."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await self._welcome.wait()
        return self._welcome.is_set()

    def public(self) -> dict[str, Any]:
        return {"provider": self.provider, "status": self.status, "error": self.error}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._code_timer is not None:
            self._code_timer.cancel()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
        self.status = "off"

    def reconnect(self) -> None:
        """The provider choice changed: drop the connection and start again."""
        if self._ws is not None:
            asyncio.get_running_loop().create_task(self._ws.close())
        self._wake.set()

    async def send(self, data: dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None or ws.closed:
            return False
        try:
            await ws.send_json(data)
            return True
        except (ConnectionError, RuntimeError, aiohttp.ClientError):
            return False

    async def code_changed(self) -> None:
        self._arm_code_timer()
        await self.send({"type": "update", "code": self.state.registration.code})

    async def tvs_changed(self) -> None:
        """A TV was paired or unpaired: the provider's list (what members see) follows."""
        await self.send({"type": "update", "tvs": await self._tvs()})

    async def unregister(self) -> None:
        """This controller leaves its provider (the page or DELETE /v1/registration)."""
        if self.state.registration.state == "registered":
            await self.send({"type": "unregister"})
        self.state.unregistered()
        self.reconnect()

    # -- the connection ------------------------------------------------------------------

    async def _run(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        pause = 2.0
        while True:
            provider = self.provider
            if not provider:
                self.status, self.error = "off", None
                self._wake.clear()
                await self._wake.wait()
                continue
            self.status = "connecting"
            try:
                await self._session_once(provider)
                pause = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 — any failure: wait and connect again
                self.error = f"{type(err).__name__}: {err}"[:200]
                log.info("provider link: %s", self.error)
            self.status = "connecting"
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(pause):
                    await self._wake.wait()
            pause = min(pause * 2, 60.0)

    async def _session_once(self, provider: str) -> None:
        reg = self.state.registration
        headers = {"User-Agent": f"orman-tv-controller/{__version__}"}
        if reg.state == "registered" and reg.provider == provider:
            headers["Authorization"] = f"Bearer {reg.credential}"
        assert self._session is not None
        try:
            ws = await self._session.ws_connect(
                connect_url(provider), headers=headers, heartbeat=self.heartbeat_s
            )
        except aiohttp.WSServerHandshakeError as err:
            if err.status == 401 and "Authorization" in headers:
                # the provider no longer knows this controller: start over, unregistered
                log.warning("provider link: the provider forgot this controller")
                self.state.unregistered()  # and forgets the provider: silent until told again
            raise
        self._ws = ws
        try:
            self.state.current_registration()
            await ws.send_json(
                {
                    "type": "hello",
                    "controller_id": self.state.id,
                    "name": self.state.name,
                    "version": __version__,
                    "code": self.state.registration.code,
                    "tvs": await self._tvs(),
                }
            )
            self._arm_code_timer()
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                await self._on(msg.json())
        finally:
            self._ws = None
            self._welcome.clear()
            self.status = "connecting"
            if not ws.closed:
                await ws.close()

    async def _tvs(self) -> list[dict[str, Any]]:
        try:
            async with asyncio.timeout(20):
                return [
                    {"id": t["id"], "name": t["name"], "capabilities": t["capabilities"]}
                    for t in await self.tvs()
                ]
        except Exception:  # noqa: BLE001 — the list is a courtesy; the hello goes without it
            return []

    def _arm_code_timer(self) -> None:
        if self._code_timer is not None:
            self._code_timer.cancel()
        wait = max(1.0, self.state.registration.expires_at - time.time() + 1)
        loop = asyncio.get_running_loop()
        self._code_timer = loop.call_later(wait, lambda: loop.create_task(self._renew_code()))

    async def _renew_code(self) -> None:
        self.state.current_registration()
        await self.code_changed()

    async def _on(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "welcome":
            self.status, self.error = "connected", None
            if not msg.get("registered") and self.state.registration.state == "registered":
                self.state.unregistered()
                self.reconnect()
            else:
                self._welcome.set()
        elif kind == "registered":
            if msg.get("credential") and msg.get("key"):
                self.state.registered(str(msg["provider"]), str(msg["credential"]), str(msg["key"]))
                log.info("registered with %s", msg["provider"])
            else:
                self.state.new_registration_code()  # another member added it; the code was used
            await self.code_changed()
        elif kind == "unregistered":
            log.info("unregistered: no member has this controller any more")
            self.state.unregistered()
            self.reconnect()  # no provider now: the link goes quiet
        elif kind == "request":
            asyncio.get_running_loop().create_task(self._relay(msg))
        elif kind == "error":
            self.error = str(msg.get("error"))
            log.warning("provider link: %s", self.error)

    async def _relay(self, msg: dict[str, Any]) -> None:
        """A member's call, away from home: run it on our own API, answer with what it said."""
        path = str(msg.get("path") or "")
        method = str(msg.get("method") or "GET").upper()
        status, body = 404, {"error": {"code": "not_found", "message": "Not a /v1 call."}}
        if path.startswith("/v1/") and method in ("GET", "POST", "DELETE"):
            url = self.local_base + path + (f"?{msg['query']}" if msg.get("query") else "")
            # marked as relayed: what needs the home network (pairing) refuses it
            headers = {"Authorization": str(msg.get("authorization") or ""), "Via": RELAY_VIA}
            try:
                assert self._session is not None
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    json=msg.get("body") if method == "POST" else None,
                    timeout=aiohttp.ClientTimeout(total=14),
                ) as resp:
                    status = resp.status
                    body = await resp.json(content_type=None)
            except Exception as err:  # noqa: BLE001
                status = 502
                body = {"error": {"code": "relay_failed", "message": str(err)[:200]}}
        await self.send({"type": "response", "id": msg.get("id"), "status": status, "body": body})

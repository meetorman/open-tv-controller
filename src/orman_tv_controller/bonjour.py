"""Bonjour: the controller says where it is as `_orman-tvctl._tcp`.

TXT record:
    id    the controller id (a client matches it against its registrations)
    api   the API version (1); the API lives under /v1
    v     the software version
    reg   1 when registered with a TV control provider, else 0

A client on the LAN browses for the type, keeps the instance whose `id` it
knows, and talks to its address and port. No home address is stored anywhere
else. An instance with reg=0 is one nobody has added yet: a client can offer
to add it, reading its code from `GET /discovery`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import ifaddr

log = logging.getLogger(__name__)

SERVICE_TYPE = "_orman-tvctl._tcp.local."


def lan_addresses() -> list[str]:
    """This host's IPv4 addresses, without loopback and link-local."""
    out = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue
            if ip.ip.startswith(("127.", "169.254.")):
                continue
            # container bridges answer nothing a client could reach
            if adapter.nice_name.startswith(("docker", "br-", "veth", "virbr", "incusbr", "lxdbr")):
                continue
            out.append(ip.ip)
    return sorted(set(out))


def txt(controller_id: str, version: str, registered: bool = False) -> dict[str, str]:
    return {"id": controller_id, "api": "1", "v": version, "reg": "1" if registered else "0"}


def service_info(
    name: str,
    controller_id: str,
    port: int,
    version: str,
    addresses: list[str] | None = None,
    *,
    registered: bool = False,
) -> Any:
    from zeroconf import ServiceInfo

    # an instance name is at most 63 bytes; the id keeps two controllers apart
    label = f"{name} {controller_id[-4:]}"[:63]
    # its own host name, so it never contends with the host's own (avahi's)
    host = f"orman-tvctl-{controller_id[-6:]}"
    return ServiceInfo(
        SERVICE_TYPE,
        f"{label}.{SERVICE_TYPE}",
        port=port,
        properties=txt(controller_id, version, registered),
        server=f"{host}.local.",
        parsed_addresses=addresses if addresses is not None else lan_addresses(),
    )


class Advertiser:
    def __init__(self, info: Any) -> None:
        self.info = info
        self._zc: Any = None
        self._updating: asyncio.Task | None = None

    async def start(self) -> None:
        from zeroconf import IPVersion
        from zeroconf.asyncio import AsyncZeroconf

        self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        # each call hands back a task that finishes once the records are out
        await (await self._zc.async_register_service(self.info, allow_name_change=True))

    def set_registered(self, registered: bool) -> None:
        """Announce the new reg= value (from inside the running loop)."""
        from zeroconf import ServiceInfo

        old = self.info
        props = {k.decode(): (v or b"").decode() for k, v in old.properties.items()}
        props["reg"] = "1" if registered else "0"
        self.info = ServiceInfo(
            old.type,
            old.name,
            port=old.port,
            properties=props,
            server=old.server,
            parsed_addresses=old.parsed_addresses(),
        )
        if self._zc is not None:
            self._updating = asyncio.get_running_loop().create_task(self._update())

    async def _update(self) -> None:
        try:
            await (await self._zc.async_update_service(self.info))
        except Exception:  # noqa: BLE001 — a missed update shows at the next query
            log.exception("bonjour: could not announce the registration change")

    async def stop(self) -> None:
        if self._updating is not None:
            with contextlib.suppress(Exception):
                await self._updating
        if self._zc is None:
            return
        try:
            # the goodbye has to go out before the socket closes
            await (await self._zc.async_unregister_service(self.info))
        finally:
            await self._zc.async_close()
            self._zc = None

"""Who counts as "on the home network".

`GET /discovery` hands out the registration code without a token: being on the
same network as the controller is the proof, the same as reading the code off
its page. So it answers only requests that come straight from a private,
link-local or loopback address, and never one that a proxy passed on.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping

# RFC 1918, link-local, IPv6 unique-local and link-local, loopback. Not CGNAT
# (100.64/10) or anything else that may span more than one home.
_LAN_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "127.0.0.0/8",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
]

# a request that names one of these came through something in between
_PROXY_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Real-IP", "Via")


def is_lan_address(remote: str | None) -> bool:
    if not remote:
        return False
    try:
        ip = ipaddress.ip_address(remote.split("%", 1)[0])  # drop an IPv6 zone (fe80::1%en0)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _LAN_NETWORKS)


def is_lan_request(remote: str | None, headers: Mapping[str, str]) -> bool:
    """Straight from the home network: a LAN source and no proxy in between."""
    if any(h in headers for h in _PROXY_HEADERS):
        return False
    return is_lan_address(remote)

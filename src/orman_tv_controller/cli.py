"""orman-tv-controller: serve, tokens, and bringing over existing pairings.

    orman-tv-controller serve [--port 8820] [--tv-address IP ...] [--fake]
    orman-tv-controller token create --name phone [--scope control] [--ttl-s N]
    orman-tv-controller token list | token revoke <id>
    orman-tv-controller import-pyatv ~/.pyatv.conf --tv-address IP
    orman-tv-controller info

Settings can also come from the environment: ORMAN_TVCTL_HOME (data folder),
ORMAN_TVCTL_PORT, ORMAN_TVCTL_BIND, ORMAN_TVCTL_NAME and
ORMAN_TVCTL_TV_ADDRESSES (comma-separated: only these TVs).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys

from . import __version__, auth, bonjour
from .state import ControllerState, data_dir

DEFAULT_PORT = 8820
SETUP_TOKEN_FILE = "setup-token"


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def _setup_token(folder, tokens: auth.LocalTokens) -> str | None:
    """The first start mints an admin token for the page, kept in a 0600 file."""
    if tokens.has_admin():
        return None
    token, _ = tokens.mint("setup", [auth.ADMIN])
    path = folder / SETUP_TOKEN_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    return token


async def serve(args: argparse.Namespace) -> None:
    from aiohttp import web

    from .api import build_app
    from .drivers.base import Driver
    from .service import Controller

    folder = data_dir(args.data)
    state = ControllerState(folder, name=args.name)
    tokens = auth.LocalTokens(folder / "tokens.json")
    fresh_token = _setup_token(folder, tokens)

    drivers: list[Driver]
    if args.fake:
        from .drivers.fake import FakeDriver

        drivers = [FakeDriver()]
    else:
        from .drivers.pyatv_driver import PyatvDriver

        drivers = [PyatvDriver(str(folder / "pyatv.conf"))]
    controller = Controller(
        drivers,
        tv_addresses=args.tv_address or None,
        scan_timeout_s=args.scan_timeout_s,
        call_timeout_s=args.call_timeout_s,
        keepalive_s=args.keepalive_s,
    )
    link = None
    if args.provider.lower() != "none":
        from .link import ProviderLink

        local = "127.0.0.1" if args.bind in ("0.0.0.0", "::", "") else args.bind
        link = ProviderLink(
            state,
            args.provider.rstrip("/") or None,  # none given: learned from the app
            f"http://{local}:{args.port}",
            lambda: controller.list_tvs(),
        )
    app = build_app(controller, state, tokens, link=link)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, args.bind, args.port)
    await site.start()
    if link is not None:
        link.start()

    addresses = bonjour.lan_addresses()
    advertiser = None
    if not args.no_bonjour:
        advertiser = bonjour.Advertiser(
            bonjour.service_info(
                state.name,
                state.id,
                args.port,
                __version__,
                addresses,
                registered=state.registration.state == "registered",
            )
        )
        await advertiser.start()
        state.on_registration_change.append(
            lambda: advertiser.set_registered(state.registration.state == "registered")
        )

    where = f"http://{addresses[0] if addresses else '127.0.0.1'}:{args.port}/"
    print(f"orman-tv-controller {__version__}: {state.name} ({state.id}) on {where}", flush=True)
    print(
        f"data: {folder}; bonjour: {'off' if args.no_bonjour else bonjour.SERVICE_TYPE};"
        f" provider: {(link.provider or 'none yet (an app names it)') if link else 'off'}",
        flush=True,
    )
    if fresh_token:
        print(f"setup page: {where}#token={fresh_token}", flush=True)
        print(f"(the setup token is also in {folder / SETUP_TOKEN_FILE})", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    if advertiser is not None:
        await advertiser.stop()
    await runner.cleanup()


def token_cmd(args: argparse.Namespace) -> int:
    folder = data_dir(args.data)
    tokens = auth.LocalTokens(folder / "tokens.json")
    if args.token_cmd == "create":
        token, row = tokens.mint(args.name, args.scope or [auth.CONTROL], args.ttl_s)
        print(json.dumps({"token": token, **row}, indent=1))
    elif args.token_cmd == "list":
        print(json.dumps(tokens.list(), indent=1))
    elif args.token_cmd == "revoke":
        if not tokens.revoke(args.id):
            print(f"no token {args.id}", file=sys.stderr)
            return 1
        print(f"revoked {args.id}")
    return 0


def info_cmd(args: argparse.Namespace) -> int:
    folder = data_dir(args.data)
    state = ControllerState(folder)
    print(
        json.dumps(
            {
                "id": state.id,
                "name": state.name,
                "access": state.access,
                "data": str(folder),
                "version": __version__,
            },
            indent=1,
        )
    )
    return 0


def import_cmd(args: argparse.Namespace) -> int:
    from .drivers.pyatv_driver import import_credentials

    folder = data_dir(args.data)
    copied = asyncio.run(
        import_credentials(args.source, str(folder / "pyatv.conf"), args.tv_address)
    )
    if not copied:
        print("no Apple TV answered at those addresses", file=sys.stderr)
        return 1
    for name in copied:
        print(f"imported {name}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orman-tv-controller", description=__doc__.split("\n")[0])
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument(
        "--data",
        default=None,
        help="data folder (default $ORMAN_TVCTL_HOME or ~/.local/share/orman-tv-controller)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the controller")
    s.add_argument("--bind", default=os.environ.get("ORMAN_TVCTL_BIND", "0.0.0.0"))
    s.add_argument(
        "--port", type=int, default=int(os.environ.get("ORMAN_TVCTL_PORT", DEFAULT_PORT))
    )
    s.add_argument("--name", default=os.environ.get("ORMAN_TVCTL_NAME") or None)
    s.add_argument(
        "--tv-address",
        action="append",
        default=_env_list("ORMAN_TVCTL_TV_ADDRESSES"),
        help="only this TV (repeat for more); default finds every TV by scan",
    )
    s.add_argument("--scan-timeout-s", type=float, default=5)
    s.add_argument("--call-timeout-s", type=float, default=10)
    s.add_argument("--keepalive-s", type=float, default=20)
    s.add_argument("--no-bonjour", action="store_true")
    s.add_argument(
        "--provider",
        default=os.environ.get("ORMAN_TVCTL_PROVIDER") or "",
        help="pin the TV control provider (its base URL) instead of learning it from the app "
        "that finds this controller; no default: nothing outbound until an app names one; "
        "none: never any outbound connection",
    )
    s.add_argument("--fake", action="store_true", help="pretend TVs, for building a client")

    t = sub.add_parser("token", help="mint, list or revoke local tokens")
    tsub = t.add_subparsers(dest="token_cmd", required=True)
    c = tsub.add_parser("create")
    c.add_argument("--name", required=True)
    c.add_argument("--scope", action="append", choices=auth.SCOPES)
    c.add_argument("--ttl-s", type=float, default=None)
    tsub.add_parser("list")
    r = tsub.add_parser("revoke")
    r.add_argument("id")

    i = sub.add_parser("import-pyatv", help="copy existing Apple TV pairings from a pyatv file")
    i.add_argument("source")
    i.add_argument("--tv-address", action="append", required=True)

    sub.add_parser("info", help="this controller's id, name and data folder")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    if args.cmd == "serve":
        asyncio.run(serve(args))
        return 0
    if args.cmd == "token":
        return token_cmd(args)
    if args.cmd == "import-pyatv":
        return import_cmd(args)
    if args.cmd == "info":
        return info_cmd(args)
    return 2

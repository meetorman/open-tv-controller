"""The HTTP API (aiohttp). Everything under /v1 needs a bearer token, except
from the home network while the controller is open (`access`, auth.py); `/` is
the controller's own page (it calls /v1 with a token like any client),
`/healthz` only says the process is up, and `/discovery` answers the home
network only (lan.py) with what a client needs to add this controller."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from importlib import resources
from typing import Any

from aiohttp import web

from . import __version__, auth, errors, lan
from . import capabilities as caps
from .link import ProviderLink
from .service import Controller
from .state import ACCESS_MODES, ACCESS_OPEN, ControllerState

log = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[Any]]

CONTROLLER: web.AppKey[Controller] = web.AppKey("controller", Controller)
STATE: web.AppKey[ControllerState] = web.AppKey("state", ControllerState)
AUTH: web.AppKey[auth.Authenticator] = web.AppKey("auth", auth.Authenticator)
TOKENS: web.AppKey[auth.LocalTokens] = web.AppKey("tokens", auth.LocalTokens)
LINK: web.AppKey[Any] = web.AppKey("link", object)
# tests shorten the wait in POST /discovery/provider
CONNECT_WAIT: web.AppKey[float] = web.AppKey("connect_wait", float)
# aiohttp 3.13+ has typed request keys; older ones take the plain string
GRANT: Any = web.RequestKey("grant", auth.Grant) if hasattr(web, "RequestKey") else "grant"


def _json(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> web.Response:
    return web.json_response(
        data, status=status, headers=headers, dumps=lambda d: json.dumps(d, indent=1)
    )


@web.middleware
async def errors_and_auth(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        if request.path.startswith("/v1/") or request.path == "/v1":
            grant = await request.app[AUTH].grant(request.headers.get("Authorization"))
            if request.app[STATE].access == ACCESS_OPEN and lan.is_lan_request(
                request.remote, request.headers
            ):
                # open: anyone on the home network, with or without a token (even a stale one)
                grant = auth.home_grant(grant)
            if grant is None:
                raise errors.Unauthorized("A valid bearer token is required.")
            request[GRANT] = grant
        return await handler(request)
    except errors.ControllerError as err:
        resp = _json(err.body(), err.status)
        if isinstance(err, errors.Unauthorized):
            resp.headers["WWW-Authenticate"] = 'Bearer realm="orman-tv-controller"'
        return resp
    except web.HTTPException:
        raise
    except Exception:
        log.exception("unhandled error on %s %s", request.method, request.path)
        return _json({"error": {"code": "internal", "message": "Something went wrong."}}, 500)


def needs(scope: str) -> Callable[[Handler], Handler]:
    def wrap(fn: Handler) -> Handler:
        async def inner(request: web.Request) -> Any:
            grant: auth.Grant = request[GRANT]
            if not grant.allows(scope):
                raise errors.Forbidden(f"This token can't do that (needs the {scope} scope).")
            return await fn(request)

        return inner

    return wrap


async def _body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise errors.BadRequest("The body isn't JSON.") from None
    if not isinstance(data, dict):
        raise errors.BadRequest("The body is a JSON object.")
    return data


# -- unauthenticated -------------------------------------------------------------


async def page(request: web.Request) -> web.Response:
    html = resources.files("orman_tv_controller.web").joinpath("index.html").read_text()
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def healthz(request: web.Request) -> web.Response:
    return _json({"ok": True})


# a code this close to expiry is renewed first, so a client has time to use it
DISCOVERY_MIN_CODE_LIFE_S = 120


async def discovery(request: web.Request) -> web.Response:
    """No token, the home network only: the name, the TVs' names and the current
    registration code, so a client that found this controller by Bonjour can add
    it in one tap (it posts the code to its provider like a typed one). Never
    relayed: the provider link passes on /v1 calls only."""
    if not lan.is_lan_request(request.remote, request.headers):
        raise errors.Forbidden("Only a client on the controller's own network can ask this.")
    state = request.app[STATE]
    reg = state.current_registration()
    if reg.expires_at - time.time() < DISCOVERY_MIN_CODE_LIFE_S:
        state.new_registration_code()
        link: ProviderLink | None = request.app[LINK]
        if link:
            await link.code_changed()
    try:
        async with asyncio.timeout(15):
            tvs = [{"name": t["name"]} for t in await request.app[CONTROLLER].list_tvs()]
    except (TimeoutError, errors.ControllerError):
        tvs = []
    reg = state.registration
    return _json(
        {
            "id": state.id,
            "name": state.name,
            "version": __version__,
            "api": 1,
            "access": state.access,
            "tvs": tvs,
            "registration": {
                "state": reg.state,
                "code": reg.code,
                "expires_at": int(reg.expires_at),
            },
            "link": _link(request),
        },
        headers={"Cache-Control": "no-store"},
    )


# how long POST /discovery/provider waits for the provider to welcome the link
DISCOVERY_CONNECT_WAIT_S = 10.0


async def discovery_provider(request: web.Request) -> web.Response:
    """No token, the home network only, like GET /discovery: the app that found
    this controller names its TV control provider, `{"provider": url}`. Until
    then the controller makes no outbound connection; now it opens the link to
    that provider, and answers once the provider has welcomed it (or after a
    few seconds) with the code to register, so the app can post it straight on.
    An unregistered controller takes a new provider; a registered one only
    agrees to its own (409 `registered` otherwise)."""
    if not lan.is_lan_request(request.remote, request.headers):
        raise errors.Forbidden("Only a client on the controller's own network can ask this.")
    state = request.app[STATE]
    link: ProviderLink | None = request.app[LINK]
    if link is None:
        raise errors.ControllerError(
            "This controller runs without a provider link (--provider none).",
            code="no_link",
            status=409,
        )
    url = _provider_url((await _body(request)).get("provider"))
    if url is None:
        raise errors.BadRequest("provider is the provider's http(s) base URL")
    reg = state.registration
    current = link.provider
    if reg.state == "registered" or link.pinned:
        if current != url:
            code = "registered" if reg.state == "registered" else "provider_pinned"
            raise errors.ControllerError(
                f"This controller uses another provider ({current}).", code=code, status=409
            )
    else:
        link.learn(url)
    await link.wait_connected(request.app.get(CONNECT_WAIT, DISCOVERY_CONNECT_WAIT_S))
    reg = state.current_registration()
    return _json(
        {
            "id": state.id,
            "registration": {
                "state": reg.state,
                "code": reg.code,
                "expires_at": int(reg.expires_at),
            },
            "link": _link(request),
        },
        headers={"Cache-Control": "no-store"},
    )


def _provider_url(url: Any) -> str | None:
    if isinstance(url, str) and url.startswith(("https://", "http://")) and len(url) < 300:
        return url.rstrip("/")
    return None


def _link(request: web.Request) -> dict[str, Any]:
    link: ProviderLink | None = request.app[LINK]
    return link.public() if link else {"provider": None, "status": "off", "error": None}


# -- controller ------------------------------------------------------------------


@needs(auth.CONTROL)
async def controller_info(request: web.Request) -> web.Response:
    state = request.app[STATE]
    ctl = request.app[CONTROLLER]
    grant: auth.Grant = request[GRANT]
    return _json(
        {
            "id": state.id,
            "name": state.name,
            "version": __version__,
            "api": 1,
            "access": state.access,
            "drivers": [d.name for d in ctl.drivers],
            "capabilities": list(caps.ALL),
            "transport_commands": list(caps.TRANSPORT_COMMANDS),
            "remote_keys": list(caps.REMOTE_KEYS_LIST),
            "registration": {
                "state": state.registration.state,
                "provider": state.registration.provider,
            },
            "you": {
                "token": grant.token_id,
                "scopes": sorted(grant.scopes),
                "issuer": grant.issuer,
            },
        }
    )


def _registration(request: web.Request) -> dict[str, Any]:
    out = request.app[STATE].current_registration().public()
    out["link"] = _link(request)
    return out


@needs(auth.ADMIN)
async def registration(request: web.Request) -> web.Response:
    return _json(_registration(request))


@needs(auth.ADMIN)
async def registration_code(request: web.Request) -> web.Response:
    request.app[STATE].new_registration_code()
    link: ProviderLink | None = request.app[LINK]
    if link:
        await link.code_changed()
    return _json(_registration(request))


@needs(auth.ADMIN)
async def registration_provider(request: web.Request) -> web.Response:
    """Choose the provider to register with (while unregistered): {"provider": url | null}.
    null forgets the choice: no outbound connection (unless the operator set --provider)."""
    state, link = request.app[STATE], request.app[LINK]
    if link is None:
        raise errors.BadRequest("This controller runs without a provider link (--provider none).")
    if state.registration.state == "registered":
        raise errors.ControllerError(
            "Registered already; unregister first.", code="registered", status=409
        )
    body = await _body(request)
    raw = body.get("provider")
    url = _provider_url(raw)
    if raw is not None and url is None:
        raise errors.BadRequest("provider is an http(s) URL, or null for none")
    state.registration.chosen_provider = url
    state.save()
    link.reconnect()
    return _json(_registration(request))


@needs(auth.ADMIN)
async def registration_delete(request: web.Request) -> web.Response:
    """Leave the provider: every member loses this controller there."""
    state, link = request.app[STATE], request.app[LINK]
    if link is not None:
        await link.unregister()
    else:
        state.unregistered()
    return _json(_registration(request))


@needs(auth.ADMIN)
async def access_get(request: web.Request) -> web.Response:
    return _json(_access(request))


@needs(auth.ADMIN)
async def access_set(request: web.Request) -> web.Response:
    """Who may use the controller from the home network: {"access": "open" | "members"}.
    `open` (the default): anyone on it, no token. `members`: a token, as away."""
    access = (await _body(request)).get("access")
    if access not in ACCESS_MODES:
        raise errors.BadRequest(f"access is {' or '.join(ACCESS_MODES)}")
    request.app[STATE].set_access(access)
    return _json(_access(request))


def _access(request: web.Request) -> dict[str, Any]:
    return {"access": request.app[STATE].access, "modes": list(ACCESS_MODES)}


# -- TVs ---------------------------------------------------------------------------


def _tv(request: web.Request) -> str:
    return request.match_info["tv"]


@needs(auth.CONTROL)
async def tvs(request: web.Request) -> web.Response:
    refresh = request.query.get("refresh", "") in ("1", "true", "yes")
    return _json({"tvs": await request.app[CONTROLLER].list_tvs(refresh=refresh)})


@needs(auth.CONTROL)
async def tv(request: web.Request) -> web.Response:
    return _json(await request.app[CONTROLLER].describe(_tv(request)))


@needs(auth.CONTROL)
async def power_get(request: web.Request) -> web.Response:
    return _json(await request.app[CONTROLLER].power(_tv(request)))


@needs(auth.CONTROL)
async def power_set(request: web.Request) -> web.Response:
    body = await _body(request)
    tv, power = _tv(request), str(body.get("power"))
    if power in ("on", "off"):
        # Record the initiator before the driver acts: a TV can sleep before
        # its remote command reply arrives. Never log tokens or request bodies.
        log.info(
            "TV power request %s",
            json.dumps(
                {
                    "tv": tv[:128],
                    "power": power,
                    "peer": (request.remote or "unknown")[:128],
                }
            ),
        )
    return _json(await request.app[CONTROLLER].set_power(tv, power))


@needs(auth.CONTROL)
async def volume_get(request: web.Request) -> web.Response:
    return _json(await request.app[CONTROLLER].volume(_tv(request)))


@needs(auth.CONTROL)
async def volume_set(request: web.Request) -> web.Response:
    body = await _body(request)
    return _json(
        await request.app[CONTROLLER].change_volume(
            _tv(request), body.get("change"), body.get("level")
        )
    )


@needs(auth.CONTROL)
async def apps(request: web.Request) -> web.Response:
    return _json(await request.app[CONTROLLER].apps(_tv(request)))


@needs(auth.CONTROL)
async def launch(request: web.Request) -> web.Response:
    body = await _body(request)
    return _json(
        await request.app[CONTROLLER].launch(_tv(request), body.get("app_id"), body.get("name"))
    )


@needs(auth.CONTROL)
async def transport(request: web.Request) -> web.Response:
    body = await _body(request)
    return _json(await request.app[CONTROLLER].transport(_tv(request), str(body.get("command"))))


@needs(auth.CONTROL)
async def remote(request: web.Request) -> web.Response:
    body = await _body(request)
    return _json(await request.app[CONTROLLER].remote_key(_tv(request), str(body.get("key"))))


def needs_pair_at_home(fn: Handler) -> Handler:
    """Pairing: an admin token (the page), or a `pair` one (a member's
    provider-minted token) on a request straight from the home network. The
    relay marks what it passes on (`Via`), so it never pairs: the PIN shows
    on the TV, and only someone in the room can read it."""

    async def inner(request: web.Request) -> Any:
        grant: auth.Grant = request[GRANT]
        if auth.ADMIN in grant.scopes:
            return await fn(request)
        if not grant.allows(auth.PAIR):
            raise errors.Forbidden("This token can't do that (needs the pair scope).")
        if not lan.is_lan_request(request.remote, request.headers):
            raise errors.Forbidden(
                "Pair a TV from the home network, next to the TV.", code="home_network_only"
            )
        return await fn(request)

    return inner


@needs_pair_at_home
async def pair_begin(request: web.Request) -> web.Response:
    step = await request.app[CONTROLLER].pair_begin(_tv(request))
    if step["done"]:
        _tvs_changed(request)
    return _json(step)


def _tvs_changed(request: web.Request) -> None:
    """Tell the provider, after the answer: learning the new capabilities takes a connection."""
    link: ProviderLink | None = request.app[LINK]
    if link is not None:
        task = asyncio.get_running_loop().create_task(link.tvs_changed())
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)


_BACKGROUND: set[asyncio.Task] = set()


@needs_pair_at_home
async def pair_pin(request: web.Request) -> web.Response:
    body = await _body(request)
    step = await request.app[CONTROLLER].pair_pin(_tv(request), str(body.get("pin", "")))
    if step["done"]:
        _tvs_changed(request)
    return _json(step)


@needs_pair_at_home
async def pair_cancel(request: web.Request) -> web.Response:
    return _json(await request.app[CONTROLLER].pair_cancel(_tv(request)))


@needs_pair_at_home
async def unpair(request: web.Request) -> web.Response:
    answer = await request.app[CONTROLLER].unpair(_tv(request))
    _tvs_changed(request)
    return _json(answer)


# -- tokens ------------------------------------------------------------------------


@needs(auth.ADMIN)
async def tokens_list(request: web.Request) -> web.Response:
    return _json({"tokens": request.app[TOKENS].list()})


@needs(auth.ADMIN)
async def tokens_mint(request: web.Request) -> web.Response:
    body = await _body(request)
    scopes = body.get("scopes") or [auth.CONTROL]
    ttl = body.get("ttl_s")
    try:
        token, row = request.app[TOKENS].mint(
            str(body.get("name") or "client"),
            list(scopes),
            float(ttl) if ttl else None,
        )
    except (TypeError, ValueError) as err:
        raise errors.BadRequest(str(err)) from None
    return _json({"token": token, **row}, 201)


@needs(auth.ADMIN)
async def tokens_revoke(request: web.Request) -> web.Response:
    token_id = request.match_info["token"]
    if token_id == request[GRANT].token_id:
        raise errors.BadRequest("A token can't revoke itself; use another admin token.")
    if not request.app[TOKENS].revoke(token_id):
        raise errors.NotFound(f"No token {token_id!r}.")
    return _json({"revoked": token_id})


def build_app(
    controller: Controller,
    state: ControllerState,
    tokens: auth.LocalTokens,
    authenticator: auth.Authenticator | None = None,
    link: ProviderLink | None = None,
) -> web.Application:
    app = web.Application(middlewares=[errors_and_auth])
    app[CONTROLLER] = controller
    app[STATE] = state
    app[TOKENS] = tokens
    app[LINK] = link
    app[AUTH] = authenticator or auth.Authenticator([tokens, auth.ProviderTokens(state)])
    r = app.router
    r.add_get("/", page)
    r.add_get("/healthz", healthz)
    r.add_get("/discovery", discovery)
    r.add_post("/discovery/provider", discovery_provider)
    r.add_get("/v1/controller", controller_info)
    r.add_get("/v1/registration", registration)
    r.add_post("/v1/registration/code", registration_code)
    r.add_put("/v1/registration/provider", registration_provider)
    r.add_delete("/v1/registration", registration_delete)
    r.add_get("/v1/access", access_get)
    r.add_put("/v1/access", access_set)
    r.add_get("/v1/tvs", tvs)
    r.add_get("/v1/tvs/{tv}", tv)
    r.add_get("/v1/tvs/{tv}/power", power_get)
    r.add_post("/v1/tvs/{tv}/power", power_set)
    r.add_get("/v1/tvs/{tv}/volume", volume_get)
    r.add_post("/v1/tvs/{tv}/volume", volume_set)
    r.add_get("/v1/tvs/{tv}/apps", apps)
    r.add_post("/v1/tvs/{tv}/apps/launch", launch)
    r.add_post("/v1/tvs/{tv}/transport", transport)
    r.add_post("/v1/tvs/{tv}/remote", remote)
    r.add_post("/v1/tvs/{tv}/pair", pair_begin)
    r.add_post("/v1/tvs/{tv}/pair/pin", pair_pin)
    r.add_delete("/v1/tvs/{tv}/pair", pair_cancel)
    r.add_post("/v1/tvs/{tv}/unpair", unpair)
    r.add_get("/v1/tokens", tokens_list)
    r.add_post("/v1/tokens", tokens_mint)
    r.add_delete("/v1/tokens/{token}", tokens_revoke)

    async def shutdown(app: web.Application) -> None:
        if link is not None:
            await link.stop()
        await controller.close()

    app.on_cleanup.append(shutdown)
    return app

# open-tv-controller

A small service that runs on a home network and controls its TVs for its
clients (the Orman app is one). A client on the same network finds it by
Bonjour and talks to it directly over HTTP. By default anyone on the home network may use it with no
token, like a TV remote app; it can be restricted to members (below). Drivers talk to the TVs; the first is
[pyatv](https://pyatv.dev) for Apple TV (devices running tvOS only: not Macs, HomePods or other AirPlay receivers). Roku or Google TV can be added as
other drivers later.

Its local API, Bonjour announcement and provider link are described below.
The package and command are `orman-tv-controller`, and it announces itself as
`_orman-tvctl._tcp`.

## Run it

It needs the home network itself (Bonjour and the TV scan are multicast).

```sh
# as a package (Linux, Raspberry Pi OS, macOS; Python 3.11+)
uv tool install git+https://github.com/meetorman/open-tv-controller@main
# or a release's wheel: uv tool install orman-tv-controller-<version>-py3-none-any.whl
orman-tv-controller serve

# as a container (linux/amd64, linux/arm64), once a release is tagged
docker run -d --name orman-tv-controller --network host --restart unless-stopped \
  -v orman-tv-controller:/data ghcr.io/meetorman/open-tv-controller:latest
```

As a user unit (starts at boot): `deploy/orman-tv-controller.service`, with
settings in `~/.config/orman-tv-controller.env` (the steps are in the file).

The first start prints a link to the controller's page with an admin token in
it (the token is also in the data folder's `setup-token` file). The page
shows the registration code, looks for TVs, and pairs Apple TVs: press Pair
and type the PIN the TV shows. An Apple TV on tvOS pairs twice (AirPlay, then
Companion), each with its own PIN. Unpair forgets a TV's pairing.

A member who has added the controller can also pair and unpair its TVs from
the app (since 0.5.0), at home only: see "Pairing from the app" below.

Settings (flags, or the environment):

| flag | environment | default |
|---|---|---|
| `--data` | `ORMAN_TVCTL_HOME` | `~/.local/share/orman-tv-controller` (`/data` in the image) |
| `--port` | `ORMAN_TVCTL_PORT` | 8820 |
| `--bind` | `ORMAN_TVCTL_BIND` | 0.0.0.0 |
| `--name` | `ORMAN_TVCTL_NAME` | `TV controller (<host>)` |
| `--tv-address IP` (repeat) | `ORMAN_TVCTL_TV_ADDRESSES` (comma-separated) | every TV the scan finds |
| `--keepalive-s` | | 20: how long a TV connection stays open after its last call |
| `--provider URL` | `ORMAN_TVCTL_PROVIDER` | none: learned from the app that finds it (below). A URL pins your provider's base URL; `none` turns the link off |
| `--fake` | | pretend TVs, for building a client without one (PIN 1234) |

Other commands:

```sh
orman-tv-controller token create --name phone            # a control token
orman-tv-controller token create --name me --scope admin
orman-tv-controller token list
orman-tv-controller token revoke <id>
orman-tv-controller import-pyatv ~/.pyatv.conf --tv-address 192.0.2.20   # reuse an existing pairing
orman-tv-controller info
```

## Access

Open by default on the home network: anyone there can use and pair its TVs
with no token, like a TV remote app. Switch `access` to `members` to require a
member's token at home too. Away from home a member's token is always needed,
and admin calls always need the admin token. The details:

The controller belongs to whoever runs it, and it decides who may use it. The
setting is `access`, kept in `controller.json`:

- `open` (the default): anyone on the home network may control and pair its
  TVs with **no token**, the way a TV remote app works. "On the home network"
  is the same check as `/discovery`: a request straight from a private,
  link-local, unique-local or loopback address, with no proxy header (so never
  the relay). A token, if one is sent, is not needed; a stale one is ignored.
- `members`: the home network needs a token too, as before 0.6.0: a member's
  provider-minted token (`control`, and `pair` at home) or a local one.

Away from home, through the provider's relay, a member's provider token is
always needed, whatever the setting. Admin calls (tokens, the registration,
this setting) always need the admin token.

Change it on the controller's page ("Who can use it", with the admin token) or
with `PUT /v1/access {"access": "open" | "members"}` (admin). `GET /discovery`
and `GET /v1/controller` say which it is (`access`).

The provider keeps only the record that members have added the controller,
and the relay for away from home: there it vouches for who the member is, and
the controller decides.

## Registration and the relay

A fresh controller has no provider and makes **no outbound connection**: it
serves its page and API on the home network and announces itself by Bonjour,
nothing else. It learns its provider from the app that finds it (since 0.4.0;
there is no built-in one):

1. The app finds it by Bonjour (`reg=0`) and reads `GET /discovery`.
2. The app sends its provider's base URL, `POST /discovery/provider
   {"provider": "<your provider's URL>"}` (no token, the home network only,
   like `/discovery`). The controller keeps it and opens its link to that
   provider, and answers once the provider has welcomed the link (or after
   10 s), with the registration code and the link's status.
3. The app posts that code to the provider, as below.

From then on it keeps one outbound WebSocket to that provider
(`<provider>/connect`), so nothing opens at home. A registered controller
keeps its provider across restarts; when it is unregistered (left on the page,
or the last member lets it go) it forgets the provider and goes quiet again.
An operator can pin a provider instead with `--provider URL`.

Its page shows a code; a
member types it into the app, which sends it to the provider. The
provider then registers the controller to that member, hands it a credential
and a token key over the connection, and keeps a pointer in the member's
storage (`controllers/<id>`). Another member adds it later with a fresh code.

At home the app can skip the typing: it finds controllers by Bonjour, offers
the ones with `reg=0` ("Found: …"), and on a tap reads the code from
`GET /discovery`, then sends it to the provider exactly like a typed one.
`/discovery` needs no token and answers only requests straight from a
private, link-local or loopback address (never one with a proxy header such
as `X-Forwarded-For`): being on the home network is the proof, as reading the
page is. The relay never passes it on (it carries `/v1` calls only).

The app asks the provider for a short-lived token (`otp1.…`, 5 minutes,
scope `control`). The controller checks it with its key, so the same token
works at home (straight to the controller, found by Bonjour) and away (through
the provider's relay, which passes the call down the connection). Provider
tokens never carry `admin`: local tokens and the registration stay with the
page. They do carry `pair`, which works on the home network only (below).

## Pairing from the app

Every token the provider mints for a member also carries the `pair` scope on
this controller. `pair` may start a TV's pairing, send its PIN, cancel it, and
unpair the TV, but only on a request straight from the home network: the
same check as `/discovery` (a private, link-local or loopback source and no
proxy header). The relay marks every call it passes on (`Via`), so pairing
never works away from home; the answer there is 403 `home_network_only`. The
PIN shows on the TV, so whoever pairs is in the room with it.
While `access` is `open` (the default) the same calls need no token at
home; with `members` they take a member's token.

The flow (the page does the same with its admin token):

1. `POST /v1/tvs/{tv}/pair` → `{"step": "airplay", "needs_pin": true, "done": false, "remaining": ["airplay", "companion"]}`; the TV shows a PIN.
2. `POST /v1/tvs/{tv}/pair/pin {"pin": "1234"}` → `{"done": false, "remaining": ["companion"]}`: start again for the next part (a new PIN), or `{"done": true, "paired": true}`.
3. `DELETE /v1/tvs/{tv}/pair` cancels a pairing in progress; `POST /v1/tvs/{tv}/unpair` forgets a paired TV's credentials (→ `{"paired": false, "to_pair": [...]}`).

Errors: 422 `pairing_failed` (wrong PIN: start again), 502 `tv_unreachable`
(the TV is asleep or off the network), 403 `home_network_only`.

When a pairing finishes or a TV is unpaired, the controller sends its
provider the new TV list (`update` with `tvs`), so every member's list
follows.

`POST /discovery/provider` takes a new provider only while unregistered (a
registered controller answers 409 `registered` for any provider but its own,
and one pinned with `--provider` 409 `provider_pinned`). With an admin token,
`PUT /v1/registration/provider {"provider": url | null}` does the same (null:
forget it); `DELETE /v1/registration` (or Unregister on the page) leaves the
provider, and forgets it.

## What it keeps

The data folder holds `controller.json` (the controller id, name,
registration code, the provider an app named, and once registered the provider, credential and token key), `tokens.json` (token hashes, never the tokens) and
`pyatv.conf` (the Apple TV pairings). All are 0600.

## Bonjour

`_orman-tvctl._tcp`, TXT `id=<controller id>`, `api=1`, `v=<version>`,
`reg=1` when registered with a provider, else `reg=0` (re-announced when it
changes).

```sh
avahi-browse -rt _orman-tvctl._tcp      # Linux
dns-sd -B _orman-tvctl._tcp             # macOS
```

## API (v1)

Every `/v1` call needs `Authorization: Bearer <token>`: a local token
(`otc_…`) or one the provider minted (`otp1.…`), except straight from the
home network while `access` is `open` (the default), where the control and
pair calls need none (above). Errors are
`{"error": {"code", "message"}}`.

| call | scope | does |
|---|---|---|
| `GET /v1/controller` | control | id, name, version, drivers, the capability names |
| `GET /v1/tvs[?refresh=1]` | control | the TVs, each with `capabilities` |
| `GET /v1/tvs/{tv}` | control | one TV |
| `GET`/`POST /v1/tvs/{tv}/power` | control | `{"power": "on"\|"off"}` |
| `GET`/`POST /v1/tvs/{tv}/volume` | control | `{"change": "up"\|"down"}` or `{"level": 0-100}` |
| `GET /v1/tvs/{tv}/apps` | control | installed apps |
| `POST /v1/tvs/{tv}/apps/launch` | control | `{"app_id": ...}` or `{"name": ...}` |
| `POST /v1/tvs/{tv}/transport` | control | `{"command": "play"\|"pause"\|"play_pause"\|"stop"\|"next"\|"previous"}` |
| `POST /v1/tvs/{tv}/remote` | control | `{"key": "up"\|"down"\|"left"\|"right"\|"select"\|"menu"\|"home"}` |
| `POST /v1/tvs/{tv}/pair`, `POST .../pair/pin`, `DELETE .../pair` | pair (home network) or admin | pairing with the TV's PIN |
| `POST /v1/tvs/{tv}/unpair` | pair (home network) or admin | forget the TV's pairing |
| `GET /v1/registration`, `POST /v1/registration/code` | admin | the registration, its code and the provider link |
| `PUT /v1/registration/provider`, `DELETE /v1/registration` | admin | pick a provider; leave it |
| `GET`/`POST /v1/tokens`, `DELETE /v1/tokens/{id}` | admin | local tokens |
| `GET`/`PUT /v1/access` | admin | `{"access": "open"\|"members"}`: who may use it at home |

`/` is the controller's page and `/healthz` says the process is up; neither
needs a token, and neither returns anything but the page or `{"ok": true}`.

`GET /discovery` needs no token and answers the home network only (else 403
`forbidden`):

```json
{"id": "ctl_…", "name": "TV controller (den)", "version": "0.6.0", "api": 1,
 "access": "open", "tvs": [{"name": "Den"}],
 "registration": {"state": "unregistered", "code": "FN92-2HG5", "expires_at": 1760000000},
 "link": {"provider": null, "status": "off", "error": null}}
```

`link.provider` null means nobody has named a provider yet: send one with
`POST /discovery/provider` before posting the code.

A code with under two minutes left is renewed first, so the one returned has
time to be used.

## Develop

```sh
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run orman-tv-controller serve --fake --port 8820
```

A tag `vX.Y.Z` (matching `pyproject.toml`) publishes the wheel on the GitHub
release and the multi-arch image on ghcr.io
(`ghcr.io/meetorman/open-tv-controller`).

## License

MIT: see `LICENSE`.

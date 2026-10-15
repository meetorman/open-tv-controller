from __future__ import annotations

from conftest import DEN, KITCHEN

from orman_tv_controller import capabilities as caps


async def test_every_v1_call_needs_a_token(rig):
    rig.state.set_access(
        "members"
    )  # tokens at home too (the default, open, lets the home network in)
    for method, path in [
        ("GET", "/v1/controller"),
        ("GET", "/v1/tvs"),
        ("GET", f"/v1/tvs/{DEN}/power"),
        ("POST", f"/v1/tvs/{DEN}/power"),
        ("GET", "/v1/tokens"),
    ]:
        r = await rig.client.request(method, path)
        assert r.status == 401, path
        assert r.headers["WWW-Authenticate"].startswith("Bearer")
        assert (await r.json())["error"]["code"] == "unauthorized"
    r = await rig.client.get("/v1/tvs", headers={"Authorization": "Bearer otc_nope.nope"})
    assert r.status == 401


async def test_page_and_health_are_open(rig):
    r = await rig.client.get("/")
    assert r.status == 200 and "TV controller" in await r.text()
    r = await rig.client.get("/healthz")
    assert (await r.json()) == {"ok": True}


async def test_controller_info(rig):
    rig.state.set_access(
        "members"
    )  # tokens at home too (the default, open, lets the home network in)
    r = await rig.client.get("/v1/controller", headers=rig.h())
    body = await r.json()
    assert body["id"] == rig.state.id and body["id"].startswith("ctl_")
    assert body["name"] == "Test controller"
    assert body["drivers"] == ["fake"]
    assert body["capabilities"] == list(caps.ALL)
    assert body["you"]["scopes"] == ["control"]


async def test_tvs_list_capabilities_per_tv(rig):
    r = await rig.client.get("/v1/tvs", headers=rig.h())
    tvs = {t["id"]: t for t in (await r.json())["tvs"]}
    den, kitchen = tvs[DEN], tvs[KITCHEN]
    assert den["paired"] and den["reachable"] and den["capabilities"] == sorted(caps.ALL)
    assert not kitchen["paired"] and kitchen["capabilities"] == []
    assert kitchen["to_pair"] == ["airplay", "companion"]
    # learnt once, then reused
    connects = rig.driver.connects
    await rig.client.get("/v1/tvs", headers=rig.h())
    assert rig.driver.connects == connects


async def test_power(rig):
    r = await rig.client.get(f"/v1/tvs/{DEN}/power", headers=rig.h())
    assert (await r.json()) == {"tv": DEN, "power": "off"}
    r = await rig.client.post(f"/v1/tvs/{DEN}/power", json={"power": "on"}, headers=rig.h())
    assert (await r.json())["power"] == "on"
    assert rig.driver.tvs["FAKE-1"].power == "on"
    r = await rig.client.post(f"/v1/tvs/{DEN}/power", json={"power": "dim"}, headers=rig.h())
    assert r.status == 400


async def test_volume(rig):
    r = await rig.client.get(f"/v1/tvs/{DEN}/volume", headers=rig.h())
    assert (await r.json())["volume"] == 30
    r = await rig.client.post(f"/v1/tvs/{DEN}/volume", json={"change": "up"}, headers=rig.h())
    assert (await r.json()) == {"tv": DEN, "change": "up", "volume": 35}
    r = await rig.client.post(f"/v1/tvs/{DEN}/volume", json={"level": 12}, headers=rig.h())
    assert (await r.json())["volume"] == 12
    r = await rig.client.post(f"/v1/tvs/{DEN}/volume", json={"level": "loud"}, headers=rig.h())
    assert r.status == 400


async def test_volume_unsupported_is_422(rig):
    rig.driver.tvs["FAKE-2"].paired = True
    await rig.controller.scan(force=True)
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/volume", json={"change": "up"}, headers=rig.h())
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "unsupported"


async def test_apps_and_launch(rig):
    r = await rig.client.get(f"/v1/tvs/{DEN}/apps", headers=rig.h())
    apps = (await r.json())["apps"]
    assert {"id": "org.videolan.vlc", "name": "VLC"} in apps
    r = await rig.client.post(
        f"/v1/tvs/{DEN}/apps/launch", json={"app_id": "com.example.tube"}, headers=rig.h()
    )
    assert r.status == 200 and rig.driver.tvs["FAKE-1"].app == "com.example.tube"
    r = await rig.client.post(f"/v1/tvs/{DEN}/apps/launch", json={"name": "vlc"}, headers=rig.h())
    assert (await r.json())["app"] == {"id": "org.videolan.vlc", "name": "VLC"}
    r = await rig.client.post(f"/v1/tvs/{DEN}/apps/launch", json={"name": "zzz"}, headers=rig.h())
    assert r.status == 404 and (await r.json())["error"]["code"] == "unknown_app"


async def test_transport_and_keys(rig):
    r = await rig.client.post(
        f"/v1/tvs/{DEN}/transport", json={"command": "pause"}, headers=rig.h()
    )
    assert r.status == 409 and (await r.json())["error"]["code"] == "unavailable"
    r = await rig.client.post(f"/v1/tvs/{DEN}/transport", json={"command": "play"}, headers=rig.h())
    assert r.status == 200 and rig.driver.tvs["FAKE-1"].playing
    r = await rig.client.post(
        f"/v1/tvs/{DEN}/transport", json={"command": "rewind"}, headers=rig.h()
    )
    assert r.status == 400
    r = await rig.client.post(f"/v1/tvs/{DEN}/remote", json={"key": "home"}, headers=rig.h())
    assert r.status == 200 and rig.driver.tvs["FAKE-1"].keys == ["home"]


async def test_unknown_and_unpaired_tv(rig):
    r = await rig.client.get("/v1/tvs/tv_000000000000/power", headers=rig.h())
    assert r.status == 404 and (await r.json())["error"]["code"] == "unknown_tv"
    r = await rig.client.get(f"/v1/tvs/{KITCHEN}/power", headers=rig.h())
    assert r.status == 409 and (await r.json())["error"]["code"] == "not_paired"


async def test_control_token_cannot_admin(rig):
    rig.state.set_access(
        "members"
    )  # tokens at home too (the default, open, lets the home network in)
    for method, path in [
        ("GET", "/v1/registration"),
        ("POST", f"/v1/tvs/{KITCHEN}/pair"),
        ("GET", "/v1/tokens"),
        ("POST", "/v1/tokens"),
    ]:
        r = await rig.client.request(method, path, headers=rig.h())
        assert r.status == 403, path


async def test_pairing_flow(rig):
    await rig.client.get("/v1/tvs", headers=rig.h())
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair", headers=rig.ha())
    step = await r.json()
    assert step["needs_pin"] and step["step"] == "airplay" and not step["done"]
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair/pin", json={"pin": "1234"}, headers=rig.ha())
    step = await r.json()
    assert not step["done"] and step["remaining"] == ["companion"]
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair", headers=rig.ha())
    assert (await r.json())["step"] == "companion"
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair/pin", json={"pin": "1234"}, headers=rig.ha())
    step = await r.json()
    assert step["done"] and step["paired"]
    r = await rig.client.get(f"/v1/tvs/{KITCHEN}", headers=rig.h())
    tv = await r.json()
    assert tv["paired"] and caps.POWER in tv["capabilities"]


async def test_pairing_wrong_pin(rig):
    await rig.client.post(f"/v1/tvs/{KITCHEN}/pair", headers=rig.ha())
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair/pin", json={"pin": "9999"}, headers=rig.ha())
    assert r.status == 422 and (await r.json())["error"]["code"] == "pairing_failed"
    r = await rig.client.post(f"/v1/tvs/{KITCHEN}/pair/pin", json={"pin": "x"}, headers=rig.ha())
    assert r.status == 400


async def test_registration_code(rig):
    r = await rig.client.get("/v1/registration", headers=rig.ha())
    reg = await r.json()
    assert reg["state"] == "unregistered" and len(reg["code"]) == 9 and reg["code"][4] == "-"
    r = await rig.client.post("/v1/registration/code", headers=rig.ha())
    assert (await r.json())["code"] != reg["code"]


async def test_tokens_mint_use_revoke(rig):
    rig.state.set_access(
        "members"
    )  # tokens at home too (the default, open, lets the home network in)
    r = await rig.client.post("/v1/tokens", json={"name": "tablet"}, headers=rig.ha())
    assert r.status == 201
    minted = await r.json()
    assert minted["scopes"] == ["control"] and minted["token"].startswith("otc_")
    r = await rig.client.get("/v1/tvs", headers=rig.h(minted["token"]))
    assert r.status == 200
    listed = (await (await rig.client.get("/v1/tokens", headers=rig.ha())).json())["tokens"]
    assert all("hash" not in t and "token" not in t for t in listed)
    r = await rig.client.delete(f"/v1/tokens/{minted['id']}", headers=rig.ha())
    assert r.status == 200
    r = await rig.client.get("/v1/tvs", headers=rig.h(minted["token"]))
    assert r.status == 401
    r = await rig.client.post("/v1/tokens", json={"scopes": ["root"]}, headers=rig.ha())
    assert r.status == 400


async def test_bad_json_body(rig):
    r = await rig.client.post(f"/v1/tvs/{DEN}/power", data="not json", headers=rig.h())
    assert r.status == 400

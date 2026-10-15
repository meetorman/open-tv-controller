from __future__ import annotations

from dataclasses import dataclass

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orman_tv_controller import auth
from orman_tv_controller.api import build_app
from orman_tv_controller.drivers.fake import FakeDriver
from orman_tv_controller.service import Controller, tv_id
from orman_tv_controller.state import ControllerState


@dataclass
class Rig:
    client: TestClient
    driver: FakeDriver
    controller: Controller
    state: ControllerState
    tokens: auth.LocalTokens
    admin: str
    control: str

    def h(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.control}"}

    def ha(self) -> dict[str, str]:
        return self.h(self.admin)


DEN = tv_id("fake", "FAKE-1")
KITCHEN = tv_id("fake", "FAKE-2")


@pytest.fixture
async def rig(tmp_path):
    driver = FakeDriver()
    controller = Controller([driver], keepalive_s=30)
    state = ControllerState(tmp_path, name="Test controller")
    tokens = auth.LocalTokens(tmp_path / "tokens.json")
    admin, _ = tokens.mint("setup", [auth.ADMIN])
    control, _ = tokens.mint("phone", [auth.CONTROL])
    client = TestClient(TestServer(build_app(controller, state, tokens)))
    await client.start_server()
    try:
        yield Rig(client, driver, controller, state, tokens, admin, control)
    finally:
        await client.close()

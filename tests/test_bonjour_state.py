from __future__ import annotations

import time

from orman_tv_controller import bonjour
from orman_tv_controller.state import ControllerState


def test_service_info_carries_the_id():
    info = bonjour.service_info(
        "Den controller", "ctl_0123456789abcdef", 8820, "0.1.0", ["192.0.2.5"]
    )
    assert info.type == "_orman-tvctl._tcp.local."
    assert info.name.endswith("._orman-tvctl._tcp.local.")
    assert info.port == 8820
    assert info.decoded_properties == {
        "id": "ctl_0123456789abcdef",
        "api": "1",
        "v": "0.1.0",
        "reg": "0",
    }
    assert info.parsed_addresses() == ["192.0.2.5"]


def test_state_keeps_id_and_renews_code(tmp_path):
    first = ControllerState(tmp_path)
    again = ControllerState(tmp_path)
    assert first.id == again.id and first.id.startswith("ctl_")
    code = again.current_registration().code
    assert again.current_registration().code == code
    renewed = again.current_registration(now=time.time() + 3600).code
    assert renewed != code
    assert (tmp_path / "controller.json").stat().st_mode & 0o777 == 0o600

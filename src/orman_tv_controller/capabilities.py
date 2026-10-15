"""The capability names: what one TV can do through this controller.

A client shows a button only when the TV lists the capability behind it. A
capability means the TV supports the thing at all; it can still be unavailable
right now (pause with nothing playing), and then the call answers 409.
"""

from __future__ import annotations

POWER = "power"  # read on/off, turn on, turn off
VOLUME_LEVEL = "volume.level"  # read the level, 0-100
VOLUME_STEP = "volume.step"  # nudge up or down
VOLUME_SET = "volume.set"  # set a level, 0-100
APPS_LIST = "apps.list"  # list installed apps
APPS_LAUNCH = "apps.launch"  # open an app by id

TRANSPORT_COMMANDS = ("play", "pause", "play_pause", "stop", "next", "previous")
TRANSPORT = tuple(f"transport.{c}" for c in TRANSPORT_COMMANDS)

REMOTE_KEYS_LIST = ("up", "down", "left", "right", "select", "menu", "home")
REMOTE_KEYS = "remote.keys"  # the arrows, select, menu (back) and home

ALL = (
    POWER,
    VOLUME_LEVEL,
    VOLUME_STEP,
    VOLUME_SET,
    APPS_LIST,
    APPS_LAUNCH,
    *TRANSPORT,
    REMOTE_KEYS,
)

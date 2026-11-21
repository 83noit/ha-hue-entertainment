"""Session start/stop races: a start during a light restore, and other clients.

Real HA harness, real EntertainmentEngine, real async_reproduce_state; the
"Zigbee" side is a fake light.turn_on/turn_off service whose latency the test
controls, so the restore window is deterministic. The first two tests failed
on v0.3.3 (reproductions from the 2026-11-21 review).
"""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant, ServiceCall  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.hue_entertainment.const import (  # noqa: E402
    CONF_API_PORT,
    CONF_BRIDGE_ID,
    CONF_ENTERTAINMENT_PORT,
    CONF_LIGHTS,
    DOMAIN,
    HUESTREAM_HEADER,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "mock_async_zeroconf")

LIGHTS = ["light.a", "light.b"]
WARM = (0.4578, 0.41)  # pre-session colour the user expects back
RED = (0.675, 0.322)  # Ambilight colour driven by the stream
GREEN = (0.17, 0.7)


@pytest.fixture(autouse=True)
def _source_ip():
    with patch("custom_components.hue_entertainment.async_get_source_ip", return_value="127.0.0.1"):
        yield


class FakeZigbeeLights:
    """light.turn_on / turn_off that update state after `delay` seconds (radio latency)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.delay = 0.0
        self.calls: list[dict] = []
        hass.services.async_register("light", "turn_on", self._turn_on)
        hass.services.async_register("light", "turn_off", self._turn_off)

    def set(self, entity_id: str, xy: tuple[float, float], bri: int = 200) -> None:
        self.hass.states.async_set(
            entity_id,
            "on",
            {
                "color_mode": "xy",
                "supported_color_modes": ["xy"],
                "xy_color": tuple(xy),
                "brightness": bri,
            },
        )

    @staticmethod
    def _ids(call: ServiceCall) -> list[str]:
        ids = call.data["entity_id"]
        return [ids] if isinstance(ids, str) else list(ids)

    async def _turn_on(self, call: ServiceCall) -> None:
        self.calls.append(dict(call.data))
        if self.delay:
            await asyncio.sleep(self.delay)
        xy = call.data.get("xy_color")
        for entity_id in self._ids(call):
            cur = self.hass.states.get(entity_id)
            self.set(
                entity_id,
                tuple(xy) if xy else cur.attributes["xy_color"],
                call.data.get("brightness", cur.attributes.get("brightness", 200)),
            )

    async def _turn_off(self, call: ServiceCall) -> None:
        self.calls.append(dict(call.data))
        for entity_id in self._ids(call):
            self.hass.states.async_set(entity_id, "off", {})

    def xy(self, entity_id: str) -> tuple[float, float]:
        return tuple(self.hass.states.get(entity_id).attributes["xy_color"])


def _xy_frame(xy: tuple[float, float], n: int = len(LIGHTS), bri: int = 0xC000) -> bytes:
    """v2 XY HueStream frame with every channel set to `xy`."""
    header = (
        HUESTREAM_HEADER + bytes([2, 0, 0, 0, 0, 1, 0]) + b"a" * 36  # v2, colorspace XY
    )
    x, y = round(xy[0] * 65535), round(xy[1] * 65535)
    return header + b"".join(bytes([i]) + struct.pack(">HHH", x, y, bri) for i in range(n))


async def _setup(hass: HomeAssistant):
    lights = FakeZigbeeLights(hass)
    for e in LIGHTS:
        lights.set(e, WARM)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Hue Entertainment",
        data={
            CONF_BRIDGE_ID: "001788FFFE0AB1C2",
            CONF_LIGHTS: LIGHTS,
            CONF_API_PORT: 0,
            CONF_ENTERTAINMENT_PORT: 0,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry, entry.runtime_data, lights


async def _stream(hass: HomeAssistant, data, xy, frames: int = 3) -> None:
    """Deliver frames the way FrameMailbox does, and let the drain loop send them."""
    for _ in range(frames):
        data.engine.handle_frame(_xy_frame(xy))
        await asyncio.sleep(0.02)


def _sensors(hass: HomeAssistant, entry) -> tuple[str, str]:
    reg = er.async_get(hass)
    return (
        reg.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_entertainment_active"),
        reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_status"),
    )


# ---------------------------------------------------------------------------
# Suspicion 2 — a start arriving while the previous session's restore runs
# ---------------------------------------------------------------------------


async def test_stop_then_start_during_restore_keeps_original_snapshot(hass: HomeAssistant) -> None:
    """Hue Essentials-style stop→start while the restore is still on the radio.

    Interleaving (main):
      1. start(A)               snapshot = WARM; frames drive lights RED
      2. stop(A)                _active=False, drain cancelled, reproduce_state(WARM) awaits radio
      3. start(A) [during 2]    _active is False → fresh snapshot = RED (restore not landed yet)
      4. restore of 2 lands     lights WARM (under the new session; frames repaint RED)
      5. stop(A)                restores the step-3 snapshot = RED  ← wrong
    """
    entry, data, lights = await _setup(hass)
    api = data.api_server

    await api._set_entertainment_active(True, "tv")
    await _stream(hass, data, RED)
    assert lights.xy("light.a") == pytest.approx(RED, abs=1e-3)

    lights.delay = 0.3  # each Zigbee command takes 300 ms to land
    stop = hass.async_create_task(api._set_entertainment_active(False, "tv"))
    await asyncio.sleep(0.05)  # restore is mid-flight
    assert lights.xy("light.a") == pytest.approx(RED, abs=1e-3)
    await api._set_entertainment_active(True, "tv")  # the app's immediate re-start
    await stop
    await _stream(hass, data, RED)

    lights.delay = 0.0
    await api._set_entertainment_active(False, "tv")
    await hass.async_block_till_done()
    assert lights.xy("light.a") == pytest.approx(WARM, abs=1e-3)  # v0.3.3: RED

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_start_during_watchdog_restore_gets_a_watchdog(hass: HomeAssistant) -> None:
    """The watchdog's own teardown blocks the next session from getting a watchdog.

    Interleaving (main):
      1. start(A); frames stop       watchdog task W polls
      2. W: silence > FRAME_TIMEOUT  W awaits _async_stop_entertainment_session → restore (slow)
      3. start(A) [during 2]         _on_entertainment_start: W is not done() → no new watchdog
      4. W's restore returns         W breaks out and exits
      5. A goes silent for good      nothing ever ends session 2: sensor stuck "streaming",
                                     no restore, and a later release() never resolves
    """
    with (
        patch("custom_components.hue_entertainment.FRAME_TIMEOUT", 0.3),
        patch("custom_components.hue_entertainment.FRAME_WATCHDOG_INTERVAL", 0.05),
    ):
        entry, data, lights = await _setup(hass)
        api = data.api_server
        _, status = _sensors(hass, entry)

        await api._set_entertainment_active(True, "tv")
        await _stream(hass, data, RED)
        lights.delay = 0.5  # restore takes 0.5 s

        # Wait until the watchdog has fired and is inside the slow restore
        for _ in range(100):
            await asyncio.sleep(0.02)
            if not data.engine.is_active:
                break
        assert not data.engine.is_active and not api.entertainment_active
        await api._set_entertainment_active(True, "tv")  # client re-starts on seeing inactive
        assert data.engine.is_active

        # Session 2 sends no frames at all; a watchdog would end it in ~0.35 s
        lights.delay = 0.0
        # (no async_block_till_done while a session may be up: it waits for the watchdog)
        await asyncio.sleep(1.5)
        assert hass.states.get(status).state == "idle"  # v0.3.3: "streaming" forever
        assert not data.engine.is_active

        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_stop_start_stop_in_one_restore_window_ends_warm(hass: HomeAssistant) -> None:
    """stop/start/stop inside one restore window: both restores now target the
    original colours, so whichever lands last the room ends up as it was."""
    entry, data, lights = await _setup(hass)
    api = data.api_server
    await api._set_entertainment_active(True, "tv")
    await _stream(hass, data, RED)
    lights.delay = 0.3
    t1 = hass.async_create_task(api._set_entertainment_active(False, "tv"))
    await asyncio.sleep(0.05)
    await api._set_entertainment_active(True, "tv")  # adopts the in-flight target (WARM)
    await _stream(hass, data, GREEN)
    await asyncio.sleep(0.4)
    t2 = hass.async_create_task(api._set_entertainment_active(False, "tv"))
    await asyncio.gather(t1, t2)
    await hass.async_block_till_done()
    restores = [tuple(c["xy_color"]) for c in lights.calls if c.get("transition") == 1.5]
    assert restores and all(xy == pytest.approx(WARM, abs=1e-3) for xy in restores)
    assert lights.xy("light.a") == pytest.approx(WARM, abs=1e-3)
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# Other clients: takeover is allowed, only the owner may stop
# ---------------------------------------------------------------------------


async def test_non_owner_stop_is_ignored(hass: HomeAssistant) -> None:
    entry, data, lights = await _setup(hass)
    api = data.api_server
    active, _ = _sensors(hass, entry)
    await api._set_entertainment_active(True, "tv_a")
    await _stream(hass, data, RED)
    await api._set_entertainment_active(False, "phone_b")
    assert api.entertainment_active and api.entertainment_owner == "tv_a"
    assert data.engine.is_active
    assert hass.states.get(active).state == "on"
    await api._set_entertainment_active(False, "tv_a")  # the owner can
    await hass.async_block_till_done()
    assert not data.engine.is_active
    assert lights.xy("light.a") == pytest.approx(WARM, abs=1e-3)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_takeover_keeps_snapshot_and_updates_owner(hass: HomeAssistant) -> None:
    entry, data, lights = await _setup(hass)
    api, engine = data.api_server, data.engine
    active, _ = _sensors(hass, entry)

    await api._set_entertainment_active(True, "tv_a")
    await _stream(hass, data, RED)
    await api._set_entertainment_active(True, "phone_b")
    assert api.entertainment_owner == "phone_b"
    assert hass.states.get(active).attributes["owner"] == "phone_b"  # was stale on v0.3.3
    assert engine._saved_states and engine._saved_states[0].attributes["xy_color"] == WARM

    await api._set_entertainment_active(False, "tv_a")  # no longer the owner: ignored
    assert engine.is_active
    await api._set_entertainment_active(False, "phone_b")
    await hass.async_block_till_done()
    assert not engine.is_active
    assert lights.xy("light.a") == pytest.approx(WARM, abs=1e-3)
    assert await hass.config_entries.async_unload(entry.entry_id)

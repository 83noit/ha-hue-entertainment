"""Integration-level tests on the Home Assistant test harness.

These need ``pytest-homeassistant-custom-component`` (see requirements_test.txt);
they are skipped under the plain nix shell.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.hue_entertainment import const  # noqa: E402
from custom_components.hue_entertainment.const import (  # noqa: E402
    CONF_API_PORT,
    CONF_BIND_IP,
    CONF_BRIDGE_ID,
    CONF_ENTERTAINMENT_PORT,
    CONF_LIGHTS,
    CONF_PAIR_NOW,
    DOMAIN,
)

BRIDGE_ID = "001788FFFE0AB1C2"
LIGHTS = ["light.a", "light.b"]

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "mock_async_zeroconf")


@pytest.fixture(autouse=True)
def _source_ip():
    with (
        patch(
            "custom_components.hue_entertainment.async_get_source_ip",
            return_value="127.0.0.1",
        ),
        patch(
            "custom_components.hue_entertainment.config_flow.async_get_source_ip",
            return_value="127.0.0.1",
        ),
    ):
        yield


def _entry(**data) -> MockConfigEntry:
    base = {
        CONF_BRIDGE_ID: BRIDGE_ID,
        CONF_LIGHTS: LIGHTS,
        CONF_API_PORT: 0,  # ephemeral ports: the tests never talk to the servers
        CONF_ENTERTAINMENT_PORT: 0,
    }
    base.update(data)
    return MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=base, title="Hue Entertainment")


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


# ---------------------------------------------------------------------------
# Setup / unload
# ---------------------------------------------------------------------------


async def test_setup_creates_device_and_sensor_and_unloads_cleanly(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    data = entry.runtime_data

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, BRIDGE_ID)})
    assert device is not None and device.model == const.BRIDGE_MODEL_ID

    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_entertainment_active"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "off"
    assert data.dtls_server._thread is not None and data.dtls_server._thread.is_alive()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not data.dtls_server._thread.is_alive()


async def test_bind_failure_raises_not_ready(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.hue_entertainment.HueAPIServer.async_start",
        side_effect=OSError(98, "Address already in use"),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


# ---------------------------------------------------------------------------
# Entertainment lifecycle
# ---------------------------------------------------------------------------


async def test_stream_start_turns_sensor_on_and_watchdog_stops_it(hass: HomeAssistant) -> None:
    with (
        patch("custom_components.hue_entertainment.FRAME_TIMEOUT", 1.0),
        patch("custom_components.hue_entertainment.FRAME_WATCHDOG_INTERVAL", 0.1),
    ):
        entry = await _setup(hass, _entry())
        data = entry.runtime_data
        entity_id = er.async_get(hass).async_get_entity_id(
            "binary_sensor", DOMAIN, f"{entry.entry_id}_entertainment_active"
        )

        await data.api_server._set_entertainment_active(True, "tvuser")
        assert hass.states.get(entity_id).state == "on"  # dispatcher delivers synchronously
        assert hass.states.get(entity_id).attributes["owner"] == "tvuser"
        assert data.engine.is_active

        # No frames arrive → watchdog auto-stops after FRAME_TIMEOUT
        await asyncio.sleep(1.6)
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == "off"
        assert not data.engine.is_active
        assert not data.api_server.entertainment_active

        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_change_reloads_entry(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    before = entry.runtime_data.api_server
    hass.config_entries.async_update_entry(entry, options={CONF_LIGHTS: ["light.c"]})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.api_server is not before
    assert entry.runtime_data.engine.stats["lights"] == ["light.c"]
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


async def test_diagnostics_redact_credentials(hass: HomeAssistant) -> None:
    from custom_components.hue_entertainment.diagnostics import (  # noqa: PLC0415
        async_get_config_entry_diagnostics,
    )

    entry = await _setup(
        hass,
        _entry(initial_users={"abcdef0123456789": {"clientkey": "deadbeef", "devicetype": "tv"}}),
    )
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"]["initial_users"] == "**REDACTED**"
    assert diag["bridge"]["paired_users"] == [{"username": "abcdef…", "devicetype": "tv"}]
    assert "deadbeef" not in str(diag)
    assert diag["engine"]["lights"] == LIGHTS
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


async def test_config_flow_pairs_and_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LIGHTS: LIGHTS}
    )
    assert result["step_id"] == "pre_pairing"

    started = {}

    async def fake_start(self):
        started["server"] = self  # no real bind on :80 in tests

    with (
        patch(
            "custom_components.hue_entertainment.config_flow.HueAPIServer.async_start", fake_start
        ),
        patch(
            "custom_components.hue_entertainment.config_flow.HueAPIServer.async_stop", AsyncMock()
        ),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["progress_action"] == "waiting_for_tv"

        # The "TV" pairs
        started["server"]._user_store.add("newuser", "cafebabe", "philips#tv")
        await asyncio.sleep(0.6)  # _wait_for_new_user polls every 0.5 s
        await hass.async_block_till_done()

        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LIGHTS] == LIGHTS
    assert result["data"]["initial_users"]["newuser"]["clientkey"] == "cafebabe"
    assert len(result["data"][CONF_BRIDGE_ID]) == 16
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.runtime_data.user_store.get_psk("newuser") == "cafebabe"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_config_flow_aborts_when_port_in_use(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LIGHTS: LIGHTS}
    )
    with patch(
        "custom_components.hue_entertainment.config_flow.HueAPIServer.async_start",
        side_effect=OSError(98, "Address already in use"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "port_in_use"


async def test_options_flow_validates_bind_ip(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LIGHTS: LIGHTS, CONF_PAIR_NOW: False, CONF_BIND_IP: "not-an-ip"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BIND_IP: "invalid_ip"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LIGHTS: LIGHTS, CONF_PAIR_NOW: False, CONF_BIND_IP: "127.0.0.1"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert dict(entry.options) == {
        CONF_LIGHTS: LIGHTS,
        CONF_BIND_IP: "127.0.0.1",
        CONF_HTTP_MODE: "auto",
        const.CONF_HTTPS_ENABLED: False,
    }
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# Hue API on Home Assistant's own HTTP server
# ---------------------------------------------------------------------------

from homeassistant.setup import async_setup_component  # noqa: E402

from custom_components.hue_entertainment.const import (  # noqa: E402
    CONF_HTTP_MODE,
    HTTP_MODE_HOMEASSISTANT,
)


async def test_ha_http_mode_serves_hue_api_on_hass_http(hass, hass_client_no_auth, hass_client):
    assert await async_setup_component(hass, "api", {})  # HA owns /api/config
    entry = await _setup(hass, _entry(**{CONF_HTTP_MODE: HTTP_MODE_HOMEASSISTANT}))
    data = entry.runtime_data
    assert data.api_server.uses_ha_http
    assert data.api_server.http_port == hass.http.server_port

    anon = await hass_client_no_auth()
    resp = await anon.get("/description.xml")
    assert resp.status == 200 and BRIDGE_ID[-6:].lower() in (await resp.text()).lower()
    resp = await anon.get("/api/nouser/config")
    assert resp.status == 200 and (await resp.json())["bridgeid"] == BRIDGE_ID
    # Unauthenticated /api/config → Hue config (shim); authenticated → HA's own
    resp = await anon.get("/api/config")
    assert resp.status == 200 and (await resp.json())["bridgeid"] == BRIDGE_ID
    authed = await hass_client()
    resp = await authed.get("/api/config")
    assert resp.status == 200 and "components" in await resp.json()
    # HA's own API still works alongside
    resp = await authed.get("/api/")
    assert resp.status == 200

    # Pairing through HA's server
    data.api_server.set_link_button(True)
    resp = await anon.post("/api", json={"devicetype": "test#tv", "generateclientkey": True})
    body = await resp.json()
    assert resp.status == 200 and "username" in body[0]["success"]
    username = body[0]["success"]["username"]
    resp = await anon.get(f"/api/{username}/lights/1")
    assert resp.status == 200 and (await resp.json())["name"] == LIGHTS[0]
    assert data.user_store.get_psk(username) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    resp = await anon.get("/api/nouser/config")
    assert resp.status == 503
    resp = await authed.get("/api/config")  # HA's handler untouched after detach
    assert resp.status == 200 and "components" in await resp.json()


async def _ha_on_port_80(hass) -> None:
    """Pretend HA serves plain HTTP on :80 (nothing is bound in the harness)."""
    assert await async_setup_component(hass, "http", {})
    hass.http.server_port = 80


async def test_auto_mode_uses_hass_http_when_ha_listens_on_80(hass):
    await _ha_on_port_80(hass)
    entry = await _setup(hass, _entry())
    assert entry.runtime_data.api_server.uses_ha_http
    assert entry.runtime_data.api_server.http_port == 80
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_auto_mode_stays_standalone_on_8123(hass):
    entry = await _setup(hass, _entry())
    assert not entry.runtime_data.api_server.uses_ha_http
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_config_flow_pairs_through_hass_http(hass, hass_client_no_auth):
    await _ha_on_port_80(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LIGHTS: LIGHTS}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    anon = await hass_client_no_auth()
    resp = await anon.post("/api", json={"devicetype": "test#tv", "generateclientkey": True})
    assert resp.status == 200 and "username" in (await resp.json())[0]["success"]
    await asyncio.sleep(0.6)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.runtime_data.api_server.uses_ha_http
    # The entry's server took over the shared views from the pairing server
    resp = await anon.get("/api/nouser/config")
    assert resp.status == 200
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("remote", "lan"),
    [
        ("10.86.2.252", True),
        ("192.168.1.20", True),
        ("172.16.0.9", True),
        ("127.0.0.1", True),
        ("fe80::1", True),
        ("fd59:f72e:dc72:d0::443", True),
        ("8.8.8.8", False),
        ("2606:4700::1111", False),
        ("", False),
        ("not-an-ip", False),
    ],
)
def test_is_lan_request(remote, lan):
    from unittest.mock import Mock  # noqa: PLC0415

    from custom_components.hue_entertainment.ha_http import is_lan_request  # noqa: PLC0415

    assert is_lan_request(Mock(remote=remote, headers={})) is lan


def test_proxied_requests_are_never_lan():
    from unittest.mock import Mock  # noqa: PLC0415

    from custom_components.hue_entertainment.ha_http import is_lan_request  # noqa: PLC0415

    assert not is_lan_request(Mock(remote="10.0.0.5", headers={"X-Forwarded-For": "10.0.0.9"}))


async def test_hosted_wildcards_do_not_capture_other_api_paths(hass, hass_client_no_auth):
    """/api/webhook/<id> etc. must not resolve to the Hue catch-alls."""
    assert await async_setup_component(hass, "api", {})
    entry = await _setup(hass, _entry(**{CONF_HTTP_MODE: HTTP_MODE_HOMEASSISTANT}))
    anon = await hass_client_no_auth()
    resp = await anon.get("/api/webhook/abc")  # no webhook view registered → plain 404, not {}
    assert resp.status == 404 and await resp.text() != "{}"
    resp = await anon.post("/api/webhook/abc", json={})
    assert resp.status == 404
    resp = await anon.get("/api/nouser/config")
    assert resp.status == 200 and "whitelist" not in await resp.json()
    # A real (32-hex) but unknown username still gets the Hue-style 401
    resp = await anon.get("/api/" + "0" * 32 + "/lights")
    assert resp.status == 200 and (await resp.json())[0]["error"]["type"] == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_hue_routes_hidden_from_non_lan_clients(hass, hass_client_no_auth, hass_client):
    assert await async_setup_component(hass, "api", {})
    entry = await _setup(hass, _entry(**{CONF_HTTP_MODE: HTTP_MODE_HOMEASSISTANT}))
    anon = await hass_client_no_auth()
    with patch("custom_components.hue_entertainment.ha_http.is_lan_request", return_value=False):
        resp = await anon.get("/api/nouser/config")
        assert resp.status == 404
        resp = await anon.get("/description.xml")
        assert resp.status == 404
        resp = await anon.get("/api/config")  # falls through to HA: 401 for anonymous
        assert resp.status == 401
        authed = await hass_client()
        resp = await authed.get("/api/config")
        assert resp.status == 200 and "components" in await resp.json()
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# pause / resume / release services and the status sensor
# ---------------------------------------------------------------------------

import voluptuous as vol  # noqa: E402
from homeassistant.exceptions import ServiceValidationError  # noqa: E402


def _sensor_ids(hass: HomeAssistant, entry: MockConfigEntry) -> tuple[str, str]:
    reg = er.async_get(hass)
    active = reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_entertainment_active"
    )
    status = reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_status")
    assert active and status
    return active, status


async def test_status_sensor_is_named_and_idle_by_default(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    _, status = _sensor_ids(hass, entry)
    assert status == "sensor.hue_entertainment_bridge_status"
    state = hass.states.get(status)
    assert state.state == "idle"
    assert state.attributes["device_class"] == "enum"
    assert set(state.attributes["options"]) == {
        "idle",
        "streaming",
        "classic",
        "paused",
        "releasing",
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_pause_and_resume_services_drive_both_sensors(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    data = entry.runtime_data
    active, status = _sensor_ids(hass, entry)

    await data.api_server._set_entertainment_active(True, "tvuser")
    assert hass.states.get(active).state == "on"
    assert hass.states.get(status).state == "streaming"

    await hass.services.async_call(DOMAIN, "pause", {"seconds": 10}, blocking=True)
    assert hass.states.get(active).state == "off"  # paused counts as "not driving"
    paused = hass.states.get(status)
    assert paused.state == "paused"
    assert 0 < paused.attributes["paused_remaining_seconds"] <= 10
    assert paused.attributes["underlying_activity"] == "streaming"
    assert data.engine.is_active  # the session itself is untouched

    await hass.services.async_call(DOMAIN, "resume", {}, blocking=True)
    assert hass.states.get(active).state == "on"
    assert hass.states.get(status).state == "streaming"

    await data.api_server._set_entertainment_active(False)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_pause_auto_expires_on_the_real_timer(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    _, status = _sensor_ids(hass, entry)
    await hass.services.async_call(DOMAIN, "pause", {"seconds": 0.2}, blocking=True)
    assert hass.states.get(status).state == "paused"
    await asyncio.sleep(0.4)
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "idle"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_release_service_forces_teardown_and_skips_restore(hass: HomeAssistant) -> None:
    """A non-compliant TV (keeps streaming) is torn down once the grace period is over,
    and the lights are NOT restored to their pre-session state — the caller's
    sweep is the new truth."""
    with (
        patch("custom_components.hue_entertainment.FRAME_TIMEOUT", 0.5),
        patch("custom_components.hue_entertainment.FRAME_WATCHDOG_INTERVAL", 0.1),
        patch(
            "custom_components.hue_entertainment.entertainment.async_reproduce_state",
            AsyncMock(),
        ) as reproduce,
    ):
        hass.states.async_set("light.a", "on", {"brightness": 200})
        entry = await _setup(hass, _entry())
        data = entry.runtime_data
        active, status = _sensor_ids(hass, entry)

        await data.api_server._set_entertainment_active(True, "tvuser")
        assert data.engine._saved_states  # snapshot taken

        await hass.services.async_call(DOMAIN, "release", {"seconds": 0.3}, blocking=True)
        assert hass.states.get(active).state == "off"
        assert hass.states.get(status).state == "releasing"
        assert not data.api_server.entertainment_active  # stream.active flag flipped
        assert data.engine.is_active  # ...but the session is still up, waiting on the TV

        await asyncio.sleep(1.2)  # grace (0.3) + watchdog check (0.1) + slack
        await hass.async_block_till_done()
        assert hass.states.get(status).state == "idle"
        assert not data.engine.is_active
        reproduce.assert_not_called()

        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_forced_release_does_not_wait_for_frame_timeout(hass: HomeAssistant) -> None:
    """Once the grace period is over the watchdog tears down on its next check,
    even with the (10 s) frame timeout far away."""
    with (
        patch("custom_components.hue_entertainment.FRAME_TIMEOUT", 60.0),
        patch("custom_components.hue_entertainment.FRAME_WATCHDOG_INTERVAL", 0.1),
    ):
        entry = await _setup(hass, _entry())
        data = entry.runtime_data
        await data.api_server._set_entertainment_active(True, "tvuser")
        await hass.services.async_call(DOMAIN, "release", {"seconds": 0.2}, blocking=True)
        await asyncio.sleep(0.6)
        await hass.async_block_till_done()
        assert not data.engine.is_active
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_session_end_aborts_the_dtls_session(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    data = entry.runtime_data
    with patch.object(data.dtls_server, "abort_session") as abort:
        await data.api_server._set_entertainment_active(True, "tvuser")
        abort.assert_not_called()
        await data.api_server._set_entertainment_active(False)
        abort.assert_called_once()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_takeover_by_another_client_aborts_the_old_stream(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    data = entry.runtime_data
    with patch.object(data.dtls_server, "abort_session") as abort:
        await data.api_server._set_entertainment_active(True, "tv")
        await data.api_server._set_entertainment_active(True, "tv")  # same client again
        abort.assert_not_called()
        await data.api_server._set_entertainment_active(True, "phone")
        abort.assert_called_once()
        assert data.api_server.entertainment_owner == "phone"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_clean_dtls_close_ends_the_session(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    data = entry.runtime_data
    await data.api_server._set_entertainment_active(True, "tvuser")
    # (no block_till_done while a session is up: it would wait for the watchdog)
    data.dtls_server._session_end_callback("idle")  # left to the watchdog
    await asyncio.sleep(0.05)
    assert data.engine.is_active
    data.dtls_server._session_end_callback("closed")
    await asyncio.sleep(0.05)
    assert not data.engine.is_active
    assert not data.api_server.entertainment_active
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_release_resolves_when_tv_complies(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    data = entry.runtime_data
    _, status = _sensor_ids(hass, entry)
    await data.api_server._set_entertainment_active(True, "tvuser")
    await hass.services.async_call(DOMAIN, "release", {"seconds": 30}, blocking=True)
    assert hass.states.get(status).state == "releasing"
    # The TV notices stream.active=false and stops on its own
    await data.api_server._set_entertainment_active(False)
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "idle"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_release_settle_seconds_blocks_the_service_call(hass: HomeAssistant) -> None:
    """End-to-end: settle_seconds is a real wait the service call itself
    blocks on, not a background timer like seconds — the caller's automation
    doesn't move on to its own next step until it elapses."""
    entry = await _setup(hass, _entry())
    started = asyncio.get_running_loop().time()
    await hass.services.async_call(
        DOMAIN, "release", {"seconds": 0, "settle_seconds": 0.3}, blocking=True
    )
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed >= 0.3
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_service_schema_caps_seconds(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "pause", {"seconds": 31}, blocking=True)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "release", {"seconds": -1}, blocking=True)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "release", {"settle_seconds": 5.1}, blocking=True)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_services_raise_when_no_bridge_is_loaded(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "pause")  # registered for HA's lifetime
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "release", {}, blocking=True)


def test_new_bridge_ids_are_unicast_eui64():
    from custom_components.hue_entertainment.config_flow import (  # noqa: PLC0415
        mac_from_bridge_id,
        new_bridge_id,
    )

    for _ in range(200):
        bridge_id = new_bridge_id()
        assert len(bridge_id) == 16 and bridge_id[6:10] == "FFFE"
        assert bridge_id == bridge_id.upper()
        assert int(bridge_id[:2], 16) & 0x01 == 0  # not a multicast MAC
        assert len(mac_from_bridge_id(bridge_id).split(":")) == 6


def test_mac_from_bridge_id_is_positional():
    from custom_components.hue_entertainment.config_flow import (  # noqa: PLC0415
        mac_from_bridge_id,
    )

    assert mac_from_bridge_id("FFFE12FFFE345678") == "ff:fe:12:34:56:78"
    assert mac_from_bridge_id("001788FFFE0AB1C2") == "00:17:88:0a:b1:c2"


# ---------------------------------------------------------------------------
# HTTPS listener (opt-in Hue Sync app support)
# ---------------------------------------------------------------------------

import socket  # noqa: E402
import ssl  # noqa: E402

from homeassistant.helpers import issue_registry as ir  # noqa: E402

from custom_components.hue_entertainment.const import (  # noqa: E402
    CONF_HTTPS_ENABLED,
    HTTPS_DISABLED,
    HTTPS_LISTENING,
    HTTPS_PORT_UNAVAILABLE,
    HTTPS_PORT_USED_BY_HA,
)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def https_port(tmp_path):
    """Ephemeral port instead of 443, certificate under tmp_path."""
    port = _free_tcp_port()
    pem = tmp_path / "bridge.pem"
    with (
        patch("custom_components.hue_entertainment.HTTPS_PORT", port),
        patch("custom_components.hue_entertainment.cert_path", return_value=pem),
    ):
        yield port


def _issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


def _peer_cert(port: int) -> dict:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        ctx.wrap_socket(raw) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    from cryptography import x509  # noqa: PLC0415

    cert = x509.load_der_x509_certificate(der)
    return {"cn": cert.subject.rfc4514_string(), "serial": cert.serial_number}


async def test_https_is_off_by_default(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    data = entry.runtime_data
    assert data.https_status == HTTPS_DISABLED
    assert not data.api_server.https_listening
    assert data.cert_fingerprint is None
    assert _issue(hass, HTTPS_PORT_USED_BY_HA) is None
    assert _issue(hass, HTTPS_PORT_UNAVAILABLE) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_https_enabled_serves_bridge_certificate(hass, https_port, tmp_path) -> None:
    entry = await _setup(hass, _entry(**{CONF_HTTPS_ENABLED: True}))
    data = entry.runtime_data
    assert data.https_status == HTTPS_LISTENING
    assert data.api_server.https_listening
    assert data.cert_fingerprint and (tmp_path / "bridge.pem").exists()

    peer = await hass.async_add_executor_job(_peer_cert, https_port)
    assert peer["cn"] == f"CN={BRIDGE_ID.lower()},O=Philips Hue,C=NL"
    assert peer["serial"] == int(BRIDGE_ID, 16)

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not data.api_server.https_listening
    assert (tmp_path / "bridge.pem").exists()  # kept across reloads
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not (tmp_path / "bridge.pem").exists()


async def test_https_skipped_when_ha_holds_443(hass, https_port) -> None:
    assert await async_setup_component(hass, "http", {})
    hass.http.server_port = 443
    entry = await _setup(hass, _entry(**{CONF_HTTPS_ENABLED: True}))
    data = entry.runtime_data
    assert data.https_status == HTTPS_PORT_USED_BY_HA
    assert not data.api_server.https_listening
    assert _issue(hass, HTTPS_PORT_USED_BY_HA) is not None
    assert data.dtls_server._thread.is_alive()  # Ambilight path unaffected

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, HTTPS_PORT_USED_BY_HA) is None


async def test_https_with_bind_ip_ignores_ha_on_443(hass, https_port) -> None:
    """A separate bridge IP is the escape hatch: try the bind instead of refusing."""
    assert await async_setup_component(hass, "http", {})
    hass.http.server_port = 443
    entry = await _setup(hass, _entry(**{CONF_HTTPS_ENABLED: True, CONF_BIND_IP: "127.0.0.1"}))
    assert entry.runtime_data.https_status == HTTPS_LISTENING
    assert _issue(hass, HTTPS_PORT_USED_BY_HA) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_https_bind_failure_raises_issue_but_entry_loads(hass, https_port) -> None:
    with patch(
        "custom_components.hue_entertainment.HueAPIServer.async_start_https",
        side_effect=OSError(98, "Address already in use"),
    ):
        entry = await _setup(hass, _entry(**{CONF_HTTPS_ENABLED: True}))
    data = entry.runtime_data
    assert data.https_status == HTTPS_PORT_UNAVAILABLE
    issue = _issue(hass, HTTPS_PORT_UNAVAILABLE)
    assert issue is not None and "Address already in use" in issue.translation_placeholders["error"]
    assert data.dtls_server._thread.is_alive()
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, HTTPS_PORT_UNAVAILABLE) is None


def _options(**extra) -> dict:
    return {CONF_LIGHTS: LIGHTS, CONF_PAIR_NOW: False, CONF_BIND_IP: "", **extra}


async def test_options_refuse_https_when_ha_holds_443(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    hass.http.server_port = 443
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _options(**{CONF_HTTPS_ENABLED: True})
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HTTPS_ENABLED: HTTPS_PORT_USED_BY_HA}
    assert CONF_HTTPS_ENABLED not in entry.options
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_refuse_https_when_port_busy(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(
        "custom_components.hue_entertainment.config_flow.probe_port",
        return_value=OSError(98, "Address already in use"),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _options(**{CONF_HTTPS_ENABLED: True})
        )
    assert result["errors"] == {CONF_HTTPS_ENABLED: HTTPS_PORT_UNAVAILABLE}
    assert "Address already in use" in result["description_placeholders"]["error"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_enable_https_starts_listener(hass, https_port) -> None:
    entry = await _setup(hass, _entry())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch("custom_components.hue_entertainment.config_flow.probe_port", return_value=None):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _options(**{CONF_HTTPS_ENABLED: True})
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.https_status == HTTPS_LISTENING

    # Already enabled (our own listener holds the port): saving again must not re-check
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _options(**{CONF_HTTPS_ENABLED: True})
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.https_status == HTTPS_LISTENING
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_report_https(hass, https_port) -> None:
    from custom_components.hue_entertainment.diagnostics import (  # noqa: PLC0415
        async_get_config_entry_diagnostics,
    )

    entry = await _setup(hass, _entry(**{CONF_HTTPS_ENABLED: True}))
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["https"]["status"] == HTTPS_LISTENING
    assert diag["https"]["certificate_sha256"] == entry.runtime_data.cert_fingerprint
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unusable_certificate_keeps_ambilight_running(hass, https_port) -> None:
    """A certificate that can't be loaded (even after regenerating) disables HTTPS
    with a repair issue; the entry and the v1/DTLS path still load."""
    with patch(
        "custom_components.hue_entertainment.create_ssl_context",
        side_effect=ssl.SSLError("bad key"),
    ):
        entry = await _setup(hass, _entry(**{CONF_HTTPS_ENABLED: True}))
    data = entry.runtime_data
    assert data.https_status == HTTPS_PORT_UNAVAILABLE
    assert "certificate" in _issue(hass, HTTPS_PORT_UNAVAILABLE).translation_placeholders["error"]
    assert not data.api_server.https_listening
    assert data.dtls_server._thread.is_alive()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_refuse_pairing_in_the_same_submit_as_enabling_https(hass, https_port):
    entry = await _setup(hass, _entry())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch("custom_components.hue_entertainment.config_flow.probe_port", return_value=None):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _options(**{CONF_HTTPS_ENABLED: True, CONF_PAIR_NOW: True})
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PAIR_NOW: "pair_after_https"}
    assert CONF_HTTPS_ENABLED not in entry.options
    assert await hass.config_entries.async_unload(entry.entry_id)

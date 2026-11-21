"""Hue Entertainment Bridge — entertainment mode for HA lights."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.components.network import async_get_source_ip
from homeassistant.components.zeroconf import async_get_async_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import CoreState, Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import get_url
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.network import is_loopback

from .certificate import certificate_fingerprint, create_ssl_context, ensure_certificate
from .config_flow import mac_from_bridge_id
from .const import (
    CONF_API_PORT,
    CONF_BIND_IP,
    CONF_BRIDGE_ID,
    CONF_ENTERTAINMENT_PORT,
    CONF_HTTP_MODE,
    CONF_HTTPS_ENABLED,
    CONF_LIGHTS,
    DEFAULT_API_PORT,
    DEFAULT_ENTERTAINMENT_PORT,
    DEFAULT_HTTP_MODE,
    DOMAIN,
    FRAME_TIMEOUT,
    FRAME_WATCHDOG_INTERVAL,
    HTTPS_DISABLED,
    HTTPS_LISTENING,
    HTTPS_PORT,
    HTTPS_PORT_UNAVAILABLE,
    HTTPS_PORT_USED_BY_HA,
    MAX_SETTLE_SECONDS,
    MAX_YIELD_SECONDS,
    SIGNAL_ENTERTAINMENT_CHANGED,
)
from .discovery import HueBridgeDiscovery
from .dtls_psk import DTLSPSKServer
from .entertainment import EntertainmentEngine, FrameMailbox, LightMapping
from .ha_http import async_get_http_host, resolve_use_ha_http
from .https import async_set_https_issue, cert_path, ha_holds_https_port
from .hue_api import HueAPIServer
from .user_store import UserStore

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

# Service names — see services.yaml for field schemas and docs/pause-release.md
# for the full contract these implement.
SERVICE_PAUSE = "pause"
SERVICE_RESUME = "resume"
SERVICE_RELEASE = "release"
ATTR_SECONDS = "seconds"
ATTR_SETTLE_SECONDS = "settle_seconds"

_SECONDS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_SECONDS, default=0): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=MAX_YIELD_SECONDS)
        ),
    }
)

# release() only: settle_seconds blocks the calling automation itself (a real
# asyncio.sleep before the service call returns), so it gets a much tighter
# cap than the seconds/MAX_YIELD_SECONDS grace period below — see
# entertainment.py's async_release docstring and const.MAX_SETTLE_SECONDS.
_RELEASE_SCHEMA = _SECONDS_SCHEMA.extend(
    {
        vol.Optional(ATTR_SETTLE_SECONDS, default=0): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=MAX_SETTLE_SECONDS)
        ),
    }
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _loaded_entries(hass: HomeAssistant) -> list[HueEntertainmentConfigEntry]:
    """The loaded bridge entries a service call fans out over.

    Raises ServiceValidationError when none is loaded (integration not set
    up, or mid-reload) so the caller's automation sees a clear error rather
    than a silent no-op.
    """
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_bridge_loaded")
    return entries


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain services (once per HA run, independent of entries).

    Services live here rather than in async_setup_entry so they exist for the
    whole lifetime of HA and never need unregister-on-last-unload bookkeeping
    (HA quality scale: "action-setup"). Each call fans out over whatever
    entries are loaded at that moment — in practice exactly one bridge.
    """

    async def _async_handle_pause(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass):
            await entry.runtime_data.engine.async_pause(call.data[ATTR_SECONDS])

    async def _async_handle_resume(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass):
            await entry.runtime_data.engine.async_resume()

    async def _async_handle_release(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass):
            # Advisory: tell a compliant TV the stream is over (it notices on
            # its next poll and disconnects on its own). Independent of
            # engine.async_release()'s own guaranteed local suppression —
            # see docs/pause-release.md.
            entry.runtime_data.api_server.clear_entertainment()
            await entry.runtime_data.engine.async_release(
                call.data[ATTR_SECONDS], call.data[ATTR_SETTLE_SECONDS]
            )

    hass.services.async_register(DOMAIN, SERVICE_PAUSE, _async_handle_pause, _SECONDS_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RESUME, _async_handle_resume)
    hass.services.async_register(DOMAIN, SERVICE_RELEASE, _async_handle_release, _RELEASE_SCHEMA)
    return True


@dataclass
class HueEntertainmentData:
    """Runtime objects for one bridge (entry.runtime_data)."""

    bridge_id: str
    api_server: HueAPIServer
    dtls_server: DTLSPSKServer
    discovery: HueBridgeDiscovery
    engine: EntertainmentEngine
    user_store: UserStore
    mailbox: FrameMailbox
    cancel_watchdog: Callable[[], None]
    https_status: str = HTTPS_DISABLED
    cert_fingerprint: str | None = None


type HueEntertainmentConfigEntry = ConfigEntry[HueEntertainmentData]


async def async_setup_entry(hass: HomeAssistant, entry: HueEntertainmentConfigEntry) -> bool:
    """Set up Hue Entertainment Bridge from a config entry."""
    light_entities: list[str] = entry.options.get(CONF_LIGHTS, entry.data.get(CONF_LIGHTS, []))

    bridge_id: str = entry.data[CONF_BRIDGE_ID]
    mac = mac_from_bridge_id(bridge_id)

    # If a bind IP is explicitly configured, use it directly — no need to probe.
    bind_ip: str | None = entry.options.get(CONF_BIND_IP) or entry.data.get(CONF_BIND_IP)
    if bind_ip:
        host_ip = bind_ip
    else:
        host_ip = await _async_get_host_ip(hass)

    # Resolve port config (options take precedence over data, data over defaults)
    ent_port = entry.options.get(
        CONF_ENTERTAINMENT_PORT,
        entry.data.get(CONF_ENTERTAINMENT_PORT, DEFAULT_ENTERTAINMENT_PORT),
    )
    http_mode = entry.options.get(CONF_HTTP_MODE, entry.data.get(CONF_HTTP_MODE, DEFAULT_HTTP_MODE))
    use_ha_http = resolve_use_ha_http(hass, http_mode)
    if use_ha_http:
        http_port = hass.http.server_port
        http_host = async_get_http_host(hass)
        if hass.http.ssl_certificate is not None or http_port != 80:
            # Explicit "homeassistant" mode on a setup the TV can't reach: Hue
            # clients only ever try plain HTTP on port 80.
            _LOGGER.warning(
                "Hue API is served by Home Assistant on %s:%d, but Hue clients expect plain HTTP "
                "on port 80 — set Home Assistant's HTTP server port to 80 without a certificate, "
                "or switch the 'Hue API server' option back to Automatic",
                "https" if hass.http.ssl_certificate else "http",
                http_port,
            )
    else:
        http_port = entry.options.get(
            CONF_API_PORT, entry.data.get(CONF_API_PORT, DEFAULT_API_PORT)
        )
        http_host = None

    # Light mappings keyed by 1-based v1 light id; the engine resolves v2
    # frames' 0-based channel ids by position in this list.
    mappings = [
        LightMapping(channel_id=i + 1, entity_id=entity_id)
        for i, entity_id in enumerate(light_entities)
    ]

    engine = EntertainmentEngine(
        hass, mappings, notify=lambda: async_dispatcher_send(hass, SIGNAL_ENTERTAINMENT_CHANGED)
    )

    # HA-idiomatic persistent user store
    ha_store: Store[dict[str, dict]] = Store(hass, version=1, key=f"{DOMAIN}.users")
    user_store = UserStore(ha_store=ha_store)
    await user_store.async_load()

    # Import users paired during the config flow's pairing step (one-time on first start)
    initial_users: dict = entry.data.get("initial_users", {})
    for username, info in initial_users.items():
        if user_store.get_psk(username) is None:
            user_store.add(username, info["clientkey"], info.get("devicetype", "unknown"))
    if initial_users:
        await user_store.async_save()

    # Opt-in HTTPS listener for official CLIP v2 clients (Hue Sync app). The
    # Ambilight TV only uses HTTP + DTLS; nothing below depends on HTTPS.
    https_enabled: bool = entry.options.get(
        CONF_HTTPS_ENABLED, entry.data.get(CONF_HTTPS_ENABLED, False)
    )
    ssl_context = None
    fingerprint: str | None = None
    if https_enabled:
        ssl_context, fingerprint = await hass.async_add_executor_job(
            _load_tls, cert_path(hass, bridge_id), bridge_id
        )

    api_server = HueAPIServer(
        bridge_id=bridge_id,
        mac=mac,
        host_ip=host_ip,
        http_port=http_port,
        channel_count=len(light_entities),
        light_entities=light_entities,
        user_store=user_store,
        bind_ip=bind_ip,
        http_host=http_host,
        ssl_context=ssl_context,
        https_port=HTTPS_PORT,
    )

    # DTLS server — always listening; TV may probe before the REST "start" action
    def psk_lookup(identity: str) -> bytes | None:
        hex_key = api_server.get_user_psk(identity)
        if hex_key is None:
            return None
        return bytes.fromhex(hex_key)

    # DTLS logs under custom_components.hue_entertainment.dtls_psk.server
    # (enable separately for handshake-level debug).
    # Frames are handed over through a single-slot mailbox (freshest wins) so a
    # stalled loop never accumulates a backlog of stale frames.
    mailbox = FrameMailbox(hass.loop, engine.handle_frame)

    @callback
    def _on_dtls_session_end(reason: str) -> None:
        # A client that closes its stream cleanly is done: end the session now
        # (as a real bridge does) instead of waiting for the frame watchdog.
        if reason == "closed" and engine.is_active:
            entry.async_create_background_task(
                hass, _on_entertainment_stop(), name=f"{DOMAIN}_dtls_closed"
            )

    dtls_server = DTLSPSKServer(
        host=bind_ip or "0.0.0.0",
        port=ent_port,
        psk_callback=psk_lookup,
        frame_callback=mailbox.put,
        loop=None,  # mailbox.put is thread-safe; it schedules onto hass.loop itself
        read_timeout=FRAME_TIMEOUT,
        session_end_callback=_on_dtls_session_end,
    )

    # mDNS discovery — use HA's shared zeroconf instance
    async_zc = await async_get_async_instance(hass)
    discovery = HueBridgeDiscovery(
        bridge_id=bridge_id,
        host_ip=host_ip,
        port=http_port,
        async_zeroconf=async_zc,
    )

    watchdog_task: asyncio.Task | None = None
    session_owner: str | None = None

    def _cancel_watchdog() -> None:
        nonlocal watchdog_task
        if watchdog_task is not None and not watchdog_task.done():
            watchdog_task.cancel()
        watchdog_task = None

    async def _async_stop_entertainment_session() -> None:
        """Tear down a session: clear the API-side flags and restore lights.

        Shared by every path that can end a session — the API-driven stop
        (`_on_entertainment_stop`), the watchdog timeout, and (indirectly, via
        `engine.async_restore_lights` itself) a release's forced teardown —
        so they can never diverge. `engine.async_restore_lights()` flips
        `engine.is_active` and notifies listeners (e.g. the sensors)
        immediately, before attempting the bounded, best-effort light
        restore.
        """
        nonlocal session_owner
        session_owner = None
        api_server.clear_entertainment()
        # End the DTLS session too, so the server is free for the next client
        # right away instead of sitting in the old read loop until it idles out.
        dtls_server.abort_session()
        await engine.async_restore_lights()

    async def _frame_watchdog() -> None:
        try:
            while engine.is_active:
                await asyncio.sleep(FRAME_WATCHDOG_INTERVAL)
                if not engine.is_active:
                    break
                # No `break` after a teardown: a session that starts while the
                # teardown is still restoring the lights finds this task not
                # done() and doesn't arm its own watchdog, so keep watching
                # (the loop ends by itself if nothing started).
                if engine.release_forcing:
                    # release() grace period over and the TV didn't comply:
                    # tear down now rather than after a full FRAME_TIMEOUT.
                    _LOGGER.info("Release grace period over, ending the session")
                    await _async_stop_entertainment_session()
                    continue
                elapsed = time.monotonic() - engine.last_frame_time
                if elapsed > FRAME_TIMEOUT:
                    _LOGGER.warning(
                        "No entertainment frames for %.1f seconds, auto-stopping", elapsed
                    )
                    await _async_stop_entertainment_session()
        except asyncio.CancelledError:
            pass

    async def _on_entertainment_start(username: str) -> None:
        nonlocal watchdog_task, session_owner
        _LOGGER.info("Entertainment started by %s…", username[:6])
        if engine.is_active and session_owner not in (None, username):
            # Another client takes over: drop the previous client's stream so
            # the newcomer's DTLS handshake isn't stuck behind it. The room's
            # "before" snapshot stays; listeners learn the new owner.
            dtls_server.abort_session()
            async_dispatcher_send(hass, SIGNAL_ENTERTAINMENT_CHANGED)
        session_owner = username
        await engine.async_snapshot_lights()
        if watchdog_task is None or watchdog_task.done():
            # Owned by the entry: cancelled automatically on unload
            watchdog_task = entry.async_create_background_task(
                hass, _frame_watchdog(), name=f"{DOMAIN}_frame_watchdog"
            )

    async def _on_entertainment_stop() -> None:
        _cancel_watchdog()
        await _async_stop_entertainment_session()

    api_server.set_entertainment_callbacks(_on_entertainment_start, _on_entertainment_stop)
    # TV "classic" mode (no DTLS stream): per-light REST commands follow the
    # same Zigbee-paced drain loop.
    api_server.set_light_command_callback(engine.handle_light_command)

    # Runtime objects for the platforms, the options flow and diagnostics
    entry.runtime_data = HueEntertainmentData(
        bridge_id=bridge_id,
        api_server=api_server,
        dtls_server=dtls_server,
        discovery=discovery,
        engine=engine,
        user_store=user_store,
        mailbox=mailbox,
        cancel_watchdog=_cancel_watchdog,
        cert_fingerprint=fingerprint,
    )

    async def _async_start_https() -> None:
        """Open :443 if enabled; on conflict raise a repair issue, never fail setup."""
        status, error = HTTPS_DISABLED, ""
        if https_enabled:
            if ha_holds_https_port(hass, bind_ip):
                status = HTTPS_PORT_USED_BY_HA
            else:
                try:
                    await api_server.async_start_https()
                    status = HTTPS_LISTENING
                except OSError as err:
                    status, error = HTTPS_PORT_UNAVAILABLE, str(err)
            if status != HTTPS_LISTENING:
                _LOGGER.warning(
                    "Hue Sync app support is enabled but port 443 is not available (%s%s); "
                    "Ambilight keeps working",
                    status,
                    f": {error}" if error else "",
                )
        entry.runtime_data.https_status = status
        async_set_https_issue(hass, status, error)

    async def _async_start(_event: Event | None = None) -> None:
        """Start servers once HA is fully running."""
        try:
            await api_server.async_start()
            await dtls_server.async_start()
        except OSError as err:
            # Port in use (typically :80) or bind IP not on this host.
            await dtls_server.async_stop()
            await api_server.async_stop()
            raise ConfigEntryNotReady(
                f"Cannot bind Hue bridge ports (http={http_port}, dtls={ent_port}): {err}"
            ) from err
        await discovery.async_start()
        await _async_start_https()
        _LOGGER.info(
            "Hue Entertainment Bridge started: bridge_id=%s, http=%s:%d, dtls=:%d, lights=%d, users=%d",
            bridge_id,
            "hass" if use_ha_http else "standalone",
            http_port,
            ent_port,
            len(light_entities),
            len(user_store.users),
        )

    async def _async_stop(event: Event) -> None:
        """Clean up on HA shutdown."""
        _cancel_watchdog()
        # Stop the servers first: HA's shutdown budget is short, and if the
        # light restore (transition) gets cut off the DTLS thread must not
        # outlive the event loop.
        await dtls_server.async_stop()
        await api_server.async_stop()
        await discovery.async_stop()
        await engine.async_restore_lights()
        _LOGGER.info("Hue Entertainment Bridge stopped")

    if hass.state is CoreState.running:
        await _async_start()
    else:

        async def _async_deferred_start(_event: Event) -> None:
            try:
                await _async_start()
            except ConfigEntryNotReady as err:
                # Too late to fail setup — surface it and let HA retry via reload.
                _LOGGER.error("%s — reloading to retry", err)
                hass.config_entries.async_schedule_reload(entry.entry_id)

        unsub_started = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _async_deferred_start
        )

        def _cancel_deferred_start() -> None:
            # A fired one-shot listener has already removed itself; removing it
            # again makes HA log "Unable to remove unknown job listener".
            if hass.state is not CoreState.running:
                unsub_started()

        entry.async_on_unload(_cancel_deferred_start)

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Options (lights, bind IP) are resolved once above — reload to apply changes
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: HueEntertainmentConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HueEntertainmentConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data = entry.runtime_data
    data.cancel_watchdog()
    # Same order as HA shutdown: stop the inputs first so no frame re-dirties a
    # slot while the lights are being restored.
    await data.dtls_server.async_stop()
    await data.api_server.async_stop()
    await data.discovery.async_stop()
    async_set_https_issue(hass, None)
    await data.engine.async_restore_lights()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: HueEntertainmentConfigEntry) -> None:
    """Delete the bridge certificate with the entry."""
    path = cert_path(hass, entry.data[CONF_BRIDGE_ID])
    await hass.async_add_executor_job(path.unlink, True)


def _load_tls(path: Path, bridge_id: str) -> tuple[ssl.SSLContext, str]:
    """Create/reuse the bridge certificate and build the server context (blocking)."""
    ensure_certificate(path, bridge_id)
    return create_ssl_context(path), certificate_fingerprint(path)


async def _async_get_host_ip(hass: HomeAssistant) -> str:
    """IP to advertise to the TV: HA's internal URL host if it is an IP, else the source IP."""
    try:
        host = urlparse(get_url(hass, prefer_external=False)).hostname
        if host and _is_ip(host) and not is_loopback(ipaddress.ip_address(host)):
            return host
    except Exception:  # noqa: BLE001
        _LOGGER.debug("No usable internal URL; using HA's source IP")
    return await async_get_source_ip(hass)


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True

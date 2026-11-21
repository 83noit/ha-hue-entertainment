"""Hue v1 REST API server for entertainment mode pairing and streaming."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import secrets
import ssl
import time
import uuid
from collections.abc import Awaitable, Callable
from ipaddress import ip_address
from typing import Any

from aiohttp import web

from . import clip_v2
from .const import (
    BRIDGE_API_VERSION,
    BRIDGE_MODEL_ID,
    BRIDGE_SW_VERSION,
    HTTPS_PORT,
    LINK_BUTTON_TIMEOUT,
)
from .user_store import UserStore

_LOGGER = logging.getLogger(__name__)

EVENTSTREAM_KEEPALIVE = 10.0  # seconds between keepalive comments on /eventstream


_PROXY_HEADERS = (
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
    "X-Real-IP",
    "Forwarded",
)


def is_lan_request(request: web.Request) -> bool:
    """True for a direct (unproxied) request from a non-globally-routable address."""
    if any(h in request.headers for h in _PROXY_HEADERS):
        # Forwarded by some proxy.  If HA trusts it, request.remote already
        # holds the real client; if it doesn't, remote is the proxy's LAN
        # address and we can't tell where the request came from.  Hue clients
        # never speak through a proxy, so refuse either way.
        return False
    remote = request.remote
    if not remote:
        return False
    try:
        addr = ip_address(remote)
    except ValueError:
        return False
    return not addr.is_global


@web.middleware
async def _lan_only(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Refuse anything that isn't a direct request from the local network.

    Hue clients live on the LAN. A port forwarded from the internet (or a
    reverse proxy) would otherwise expose pairing and the lights to anyone.
    """
    if not is_lan_request(request):
        _LOGGER.debug("Refused non-LAN request from %s: %s", request.remote, request.path)
        raise web.HTTPNotFound
    return await handler(request)


@web.middleware
async def _request_logger(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Log every request at DEBUG — shows exactly which endpoints the TV hits."""
    resp = await handler(request)
    _LOGGER.debug("%s %s -> %d", request.method, _log_path(request), resp.status)
    return resp


def short_id(username: str | None) -> str:
    """A username is the client's credential: log only its first characters."""
    return "-" if not username else f"{username[:6]}…"


def _log_path(request: web.Request) -> str:
    username = request.match_info.get("username")
    return request.path.replace(username, short_id(username)) if username else request.path


class HueAPIServer:
    """Serve the Hue v1 API endpoints needed for TV entertainment mode."""

    LINK_BUTTON_TIMEOUT = LINK_BUTTON_TIMEOUT

    def __init__(
        self,
        bridge_id: str,
        mac: str,
        host_ip: str,
        http_port: int,
        channel_count: int,
        light_entities: list[str],
        user_store: UserStore | None = None,
        bind_ip: str | None = None,
        http_host: Any = None,
        ssl_context: ssl.SSLContext | None = None,
        https_port: int = HTTPS_PORT,
    ) -> None:
        self._bridge_id = bridge_id
        self._mac = mac
        self._host_ip = host_ip
        self._http_port = http_port
        self._channel_count = channel_count
        self._light_entities = light_entities
        self._bind_ip = bind_ip
        # ha_http.HueHttpHost: when set, requests arrive through Home Assistant's
        # own HTTP server instead of a standalone aiohttp server on http_port.
        self._http_host = http_host

        self._user_store = user_store if user_store is not None else UserStore()

        # Link button state — timed window, just like the real bridge
        self._link_button_expires = 0.0

        # Entertainment streaming state
        self._entertainment_active = False
        self._entertainment_owner: str | None = None

        # Callbacks
        self._on_entertainment_start: Callable[[str], Awaitable[None]] | None = None
        self._on_entertainment_stop: Callable[[], Awaitable[None]] | None = None
        self._on_light_command: Callable[[int, dict], None] | None = None

        self._http_runner: web.AppRunner | None = None
        # Opt-in HTTPS listener (official CLIP v2 clients); always standalone,
        # since it must present the bridge-shaped certificate.
        self._ssl_context = ssl_context
        self._https_port = https_port
        self._https_runner: web.AppRunner | None = None
        self._clip_logged_paths: set[str] = set()
        # One queue per open /eventstream/clip/v2 connection; None ends it
        self._event_queues: set[asyncio.Queue[dict | None]] = set()

    def set_entertainment_callbacks(
        self,
        on_start: Callable[[str], Awaitable[None]],
        on_stop: Callable[[], Awaitable[None]],
    ) -> None:
        """Set callbacks for entertainment start/stop."""
        self._on_entertainment_start = on_start
        self._on_entertainment_stop = on_stop

    def set_light_command_callback(self, callback: Callable[[int, dict], None]) -> None:
        """Receive v1 ``PUT /lights/{id}/state`` bodies (TV classic mode)."""
        self._on_light_command = callback

    @property
    def entertainment_active(self) -> bool:
        """Whether entertainment streaming is currently active."""
        return self._entertainment_active

    @property
    def entertainment_owner(self) -> str | None:
        """Username of the client that activated entertainment, or None."""
        return self._entertainment_owner

    def clear_entertainment(self) -> None:
        """Reset entertainment state (e.g. from watchdog timeout)."""
        was_active = self._entertainment_active
        self._entertainment_active = False
        self._entertainment_owner = None
        if was_active:
            self._publish_configuration_update()

    @property
    def _link_button_active(self) -> bool:
        return time.monotonic() < self._link_button_expires

    def set_link_button(self, active: bool) -> None:
        """Enable the link button for 30 seconds, or disable it immediately."""
        if active:
            self._link_button_expires = time.monotonic() + self.LINK_BUTTON_TIMEOUT
        else:
            self._link_button_expires = 0.0

    def get_user_psk(self, username: str) -> str | None:
        """Get the PSK (clientkey) for a username."""
        return self._user_store.get_psk(username)

    @property
    def http_port(self) -> int:
        """Port the Hue API is reachable on."""
        return self._http_port

    @property
    def uses_ha_http(self) -> bool:
        """True when served through Home Assistant's HTTP server."""
        return self._http_host is not None

    async def async_start(self) -> None:
        """Start serving: attach to HA's HTTP server, or bind a standalone one."""
        if self._http_host is not None:
            self._http_host.attach(self)
            _LOGGER.info("Hue API served by Home Assistant's HTTP server on :%d", self._http_port)
            return
        app = web.Application(middlewares=[_lan_only, _request_logger])
        self._register_routes(app)

        self._http_runner = web.AppRunner(app)
        await self._http_runner.setup()
        bind_addr = self._bind_ip or "0.0.0.0"
        http_site = web.TCPSite(self._http_runner, bind_addr, self._http_port)
        await http_site.start()
        _LOGGER.info("Hue API HTTP server on %s:%d", bind_addr, self._http_port)

    @property
    def https_listening(self) -> bool:
        """Whether the HTTPS listener is up."""
        return self._https_runner is not None

    async def async_start_https(self) -> None:
        """Open the HTTPS listener; raises OSError if the port can't be bound."""
        if self._ssl_context is None or self._https_runner is not None:
            return
        app = web.Application(middlewares=[_request_logger])
        self._register_routes(app, clip=True)
        runner = web.AppRunner(app)
        await runner.setup()
        bind_addr = self._bind_ip or "0.0.0.0"
        site = web.TCPSite(runner, bind_addr, self._https_port, ssl_context=self._ssl_context)
        try:
            await site.start()
        except OSError:
            await runner.cleanup()
            raise
        self._https_runner = runner
        _LOGGER.info("Hue API HTTPS server on %s:%d", bind_addr, self._https_port)

    async def async_stop(self) -> None:
        """Stop serving."""
        for queue in self._event_queues:
            queue.put_nowait(None)  # end open event streams so cleanup doesn't wait on them
        if self._http_host is not None:
            self._http_host.detach(self)
        if self._http_runner:
            await self._http_runner.cleanup()
            self._http_runner = None
        if self._https_runner:
            await self._https_runner.cleanup()
            self._https_runner = None
        _LOGGER.info("Hue API server stopped")

    # (method, path, handler attribute) — shared by the standalone aiohttp app and
    # the views registered on Home Assistant's own HTTP server (see ha_http.py).
    ROUTES: tuple[tuple[str, str, str], ...] = (
        # UPnP description
        ("GET", "/description.xml", "_handle_description_xml"),
        # Unauthenticated config (two paths the TV may try)
        ("GET", "/api/nouser/config", "_handle_config"),
        ("GET", "/api/config", "_handle_config"),
        ("POST", "/api", "_handle_create_user"),
        ("GET", "/api/{username}", "_handle_full_datastore"),
        ("GET", "/api/{username}/config", "_handle_config_auth"),
        ("GET", "/api/{username}/capabilities", "_handle_capabilities"),
        # V1 lights and groups (TV reads these to find colour bulbs + entertainment areas)
        ("GET", "/api/{username}/lights", "_handle_v1_lights"),
        ("GET", "/api/{username}/lights/{light_id}", "_handle_v1_light_by_id"),
        ("PUT", "/api/{username}/lights/{light_id}/state", "_handle_v1_light_state"),
        ("GET", "/api/{username}/groups", "_handle_v1_groups"),
        ("GET", "/api/{username}/groups/{group_id}", "_handle_v1_group_by_id"),
        ("PUT", "/api/{username}/groups/{group_id}", "_handle_v1_group_put"),
        ("PUT", "/api/{username}/groups/{group_id}/stream", "_handle_v1_stream"),
        # Catch-alls for unimplemented v1 resources (avoids 404s / 405s)
        ("GET", "/api/{username}/{resource}", "_handle_v1_catchall"),
        ("GET", "/api/{username}/{resource}/{id}", "_handle_v1_catchall"),
        ("PUT", "/api/{username}/{resource}/{id}", "_handle_v1_put_catchall"),
        ("PUT", "/api/{username}/{resource}/{id}/{param}", "_handle_v1_put_catchall"),
        ("POST", "/api/{username}/{resource}", "_handle_v1_post_catchall"),
        ("DELETE", "/api/{username}/{resource}/{id}", "_handle_v1_delete_catchall"),
    )

    # CLIP v2, HTTPS listener only (real bridges don't serve it over plain HTTP)
    CLIP_ROUTES: tuple[tuple[str, str, str], ...] = (
        ("GET", "/auth/v1", "_handle_auth_v1"),
        ("GET", "/clip/v2/resource", "_handle_clip_all"),
        ("GET", "/clip/v2/resource/{rtype}", "_handle_clip_rtype"),
        ("GET", "/clip/v2/resource/{rtype}/{rid}", "_handle_clip_by_id"),
        ("PUT", "/clip/v2/resource/entertainment_configuration/{rid}", "_handle_clip_ent_put"),
        ("GET", "/eventstream/clip/v2", "_handle_eventstream"),
        # Anything else under /clip/v2 (creating/editing resources): 405, logged
        ("*", "/clip/v2/{tail:.*}", "_handle_clip_unsupported"),
    )

    def _register_routes(self, app: web.Application, clip: bool = False) -> None:
        """Register the v1 routes (and CLIP v2 ones on HTTPS) on a standalone app."""
        for method, path, attr in self.ROUTES + (self.CLIP_ROUTES if clip else ()):
            app.router.add_route(method, path, getattr(self, attr))

    async def handle(self, attr: str, request: web.Request) -> web.Response:
        """Dispatch a request to a handler by attribute name (used by ha_http views)."""
        handler: Callable[[web.Request], Awaitable[web.Response]] = getattr(self, attr)
        resp = await handler(request)
        _LOGGER.debug("%s %s -> %d", request.method, _log_path(request), resp.status)
        return resp

    # ---------------------------------------------------------------------------
    # Data builders
    # ---------------------------------------------------------------------------

    def _build_v1_config(self) -> dict:
        return {
            "name": "Hue Entertainment Bridge",
            "datastoreversion": "163",
            "swversion": BRIDGE_SW_VERSION,
            "apiversion": BRIDGE_API_VERSION,
            "mac": self._mac,
            "bridgeid": self._bridge_id.upper(),
            "factorynew": False,
            "replacesbridgeid": None,
            "modelid": BRIDGE_MODEL_ID,
            "starterkitid": "",
            "ipaddress": self._host_ip,
            "dhcp": True,
            "netmask": "255.255.255.0",
            "gateway": self._host_ip.rsplit(".", 1)[0] + ".1",
            "UTC": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "localtime": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": "Europe/London",
            "zigbeechannel": 25,
            "linkbutton": self._link_button_active,
            "portalservices": False,
            "portalstate": {
                "signedon": False,
                "incoming": False,
                "outgoing": False,
                "communication": "disconnected",
            },
            "internetservices": {
                "internet": "disconnected",
                "remoteaccess": "disconnected",
                "swupdate": "disconnected",
                "time": "disconnected",
            },
            "swupdate2": {
                "checkforupdate": False,
                "state": "noupdates",
                "autoinstall": {"on": False, "updatetime": "T14:00:00"},
            },
            "backup": {"errorcode": 0, "status": "idle"},
            "whitelist": {
                u: {
                    "create date": "2026-01-01T00:00:00",
                    "last use date": "2026-01-01T00:00:00",
                    "name": info.get("devicetype", "unknown"),
                }
                for u, info in self._user_store.users.items()
            },
        }

    def _build_v1_lights(self) -> dict:
        lights = {}
        for i, entity_id in enumerate(self._light_entities, 1):
            h = uuid.uuid5(uuid.NAMESPACE_DNS, f"uniqueid-{entity_id}").hex
            uniqueid = ":".join(h[j : j + 2] for j in range(0, 16, 2)) + "-0b"
            lights[str(i)] = {
                "state": {
                    "on": True,
                    "bri": 254,
                    "hue": 0,
                    "sat": 0,
                    "xy": [0.0, 0.0],
                    "ct": 500,
                    "alert": "select",
                    "effect": "none",
                    "colormode": "xy",
                    "mode": "homeautomation",
                    "reachable": True,
                },
                "type": "Extended color light",
                "name": entity_id,
                "modelid": "LCT015",
                "manufacturername": "Signify Netherlands B.V.",
                "productname": "Hue color lamp",
                "capabilities": {
                    "certified": True,
                    "control": {
                        "mindimlevel": 200,
                        "maxlumen": 800,
                        "colorgamuttype": "C",
                        "colorgamut": [
                            [0.6915, 0.3083],
                            [0.17, 0.7],
                            [0.1532, 0.0475],
                        ],
                        "ct": {"min": 153, "max": 500},
                    },
                    "streaming": {"renderer": True, "proxy": False},
                },
                "uniqueid": uniqueid,
                "swversion": "1.104.2",
            }
        return lights

    def _build_v1_groups(self) -> dict:
        if not self._light_entities:
            return {}
        light_ids = [str(i) for i in range(1, len(self._light_entities) + 1)]
        positions = clip_v2.synthetic_positions(len(self._light_entities))
        locations = dict(zip(light_ids, positions, strict=True))
        return {
            "1": {
                "name": clip_v2.ENTERTAINMENT_GROUP_NAME,
                "lights": light_ids,
                "sensors": [],
                "type": "Entertainment",
                "class": "TV",
                "stream": {
                    "proxymode": "auto",
                    "proxynode": f"/lights/{light_ids[0]}",
                    "active": self.entertainment_active,
                    "owner": self.entertainment_owner,
                },
                "locations": locations,
                "state": {"any_on": True, "all_on": True},
                "action": {
                    "on": True,
                    "bri": 254,
                    "hue": 0,
                    "sat": 0,
                    "effect": "none",
                    "xy": [0.0, 0.0],
                    "ct": 500,
                    "alert": "none",
                    "colormode": "xy",
                },
            }
        }

    # ---------------------------------------------------------------------------
    # Request handlers
    # ---------------------------------------------------------------------------

    async def _handle_description_xml(self, request: web.Request) -> web.Response:
        """GET /description.xml — UPnP device description."""
        serial = self._mac.replace(":", "").lower()
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<root xmlns="urn:schemas-upnp-org:device-1-0">\n'
            "<specVersion><major>1</major><minor>0</minor></specVersion>\n"
            f"<URLBase>http://{self._host_ip}:{self._http_port}/</URLBase>\n"
            "<device>\n"
            "<deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType>\n"
            f"<friendlyName>Philips hue ({self._host_ip})</friendlyName>\n"
            "<manufacturer>Signify</manufacturer>\n"
            "<manufacturerURL>http://www.meethue.com</manufacturerURL>\n"
            "<modelDescription>Philips hue Personal Wireless Lighting</modelDescription>\n"
            "<modelName>Philips hue bridge 2015</modelName>\n"
            "<modelNumber>BSB002</modelNumber>\n"
            "<modelURL>http://www.meethue.com</modelURL>\n"
            f"<serialNumber>{serial}</serialNumber>\n"
            f"<UDN>uuid:2f402f80-da50-11e1-9b23-{serial}</UDN>\n"
            "<presentationURL>index.html</presentationURL>\n"
            "</device>\n"
            "</root>"
        )
        return web.Response(text=xml, content_type="text/xml")

    def _check_username(self, request: web.Request) -> str | None:
        """Return username if valid, or None if unknown."""
        username = request.match_info.get("username", "")
        if self._user_store.get_psk(username) is not None:
            return username
        return None

    @staticmethod
    def _unauthorized_response() -> web.Response:
        """Return a Hue-style 'unauthorized user' error response."""
        return web.json_response(
            [{"error": {"type": 1, "address": "/", "description": "unauthorized user"}}]
        )

    @staticmethod
    async def _safe_json(request: web.Request) -> dict | None:
        """Parse a JSON object body, returning None on malformed input or a non-object."""
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None

    async def _set_entertainment_active(self, active: bool, username: str = "") -> None:
        """Toggle entertainment streaming state and fire callbacks.

        Another client may take over a session (a new start), but only the
        owner may stop it: a stop from anyone else is ignored, so a second app
        can't end the TV's session from under it.
        """
        if (
            not active
            and username
            and self._entertainment_active
            and self._entertainment_owner
            and username != self._entertainment_owner
        ):
            _LOGGER.warning(
                "Ignoring a stream stop from %s: the session belongs to %s",
                short_id(username),
                short_id(self._entertainment_owner),
            )
            return
        if active:
            self._entertainment_active = True
            self._entertainment_owner = username
            _LOGGER.info("Entertainment streaming started by %s", short_id(username))
            self._publish_configuration_update()
            if self._on_entertainment_start:
                await self._on_entertainment_start(username)
        else:
            self._entertainment_active = False
            self._entertainment_owner = None
            _LOGGER.info("Entertainment streaming stopped")
            self._publish_configuration_update()
            if self._on_entertainment_stop:
                await self._on_entertainment_stop()

    async def _handle_full_datastore(self, request: web.Request) -> web.Response:
        """GET /api/{username} — v1 full datastore."""
        username = self._check_username(request)
        if username is None:
            return self._unauthorized_response()
        return web.json_response(
            {
                "lights": self._build_v1_lights(),
                "groups": self._build_v1_groups(),
                "config": self._build_v1_config(),
                "schedules": {},
                "scenes": {},
                "rules": {},
                "sensors": {},
                "resourcelinks": {},
            }
        )

    async def _handle_config(self, request: web.Request) -> web.Response:
        """GET /api/nouser/config and /api/config — basic bridge info for discovery.

        Like a real bridge, the unauthenticated config omits ``whitelist``: a
        username is the only credential the REST API checks.
        """
        config = self._build_v1_config()
        config.pop("whitelist", None)
        return web.json_response(config)

    async def _handle_config_auth(self, request: web.Request) -> web.Response:
        """GET /api/{username}/config — full config, or the public one for an unknown user.

        A real bridge answers an unknown username here with the same short
        public config as /api/config, not an error; clients use it to check a
        bridge before (re-)pairing.
        """
        if self._check_username(request) is None:
            return await self._handle_config(request)
        return web.json_response(self._build_v1_config())

    async def _handle_capabilities(self, request: web.Request) -> web.Response:
        """GET /api/{username}/capabilities."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        return web.json_response(
            {
                "lights": {"available": 50, "total": 50},
                "sensors": {"available": 250, "total": 250},
                "groups": {"available": 64, "total": 64},
                "scenes": {"available": 200, "total": 200},
                "schedules": {"available": 100, "total": 100},
                "rules": {"available": 250, "total": 250},
                "resourcelinks": {"available": 64, "total": 64},
                "streaming": {
                    "available": 1,
                    "total": 1,
                    "channels": self._channel_count,
                },
                "timezones": {"values": []},
            }
        )

    async def _handle_create_user(self, request: web.Request) -> web.Response:
        """POST /api — pair a new client (link button press).

        Idempotent: if a user with the same devicetype already exists, return
        its credentials instead of creating a duplicate.  This handles TVs that
        fire a burst of POST /api requests — every response carries the same
        username/clientkey so the TV gets consistent credentials regardless of
        which response it uses.
        """
        body = await self._safe_json(request)
        if body is None:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 2,
                            "address": "/",
                            "description": "body contains invalid json",
                        }
                    }
                ]
            )
        device_type = body.get("devicetype", "unknown")
        generate_clientkey = body.get("generateclientkey", False)

        if not self._link_button_active:
            return web.json_response(
                [{"error": {"type": 101, "address": "", "description": "link button not pressed"}}]
            )

        # Return existing credentials if this devicetype already paired
        existing = self._user_store.get_by_devicetype(device_type)
        if existing is not None:
            username, clientkey = existing
        else:
            username = uuid.uuid4().hex[:32]
            clientkey = secrets.token_hex(16)
            self._user_store.add(username, clientkey, device_type)
            await self._user_store.async_save()
            _LOGGER.info("Paired new client: %s (%s)", device_type, short_id(username))

        result = {"success": {"username": username}}
        if generate_clientkey:
            result["success"]["clientkey"] = clientkey

        return web.json_response([result])

    async def _handle_v1_lights(self, request: web.Request) -> web.Response:
        """GET /api/{username}/lights."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        return web.json_response(self._build_v1_lights())

    async def _handle_v1_groups(self, request: web.Request) -> web.Response:
        """GET /api/{username}/groups."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        return web.json_response(self._build_v1_groups())

    async def _handle_v1_light_state(self, request: web.Request) -> web.Response:
        """PUT /api/{username}/lights/{light_id}/state — accept light state changes."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        light_id = request.match_info["light_id"]
        body = await self._safe_json(request)
        if body is None:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 2,
                            "address": "/",
                            "description": "body contains invalid json",
                        }
                    }
                ]
            )
        _LOGGER.debug("Light %s state update: %s", light_id, body)
        if self._on_light_command is not None and light_id.isdigit():
            self._on_light_command(int(light_id), body)
        result = [{"success": {f"/lights/{light_id}/state/{k}": v}} for k, v in body.items()]
        return web.json_response(result)

    async def _handle_v1_light_by_id(self, request: web.Request) -> web.Response:
        """GET /api/{username}/lights/{light_id} — single light object."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        light_id = request.match_info["light_id"]
        lights = self._build_v1_lights()
        if light_id not in lights:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 3,
                            "address": f"/lights/{light_id}",
                            "description": f"resource, /lights/{light_id}, not available",
                        }
                    }
                ],
                status=404,
            )
        return web.json_response(lights[light_id])

    async def _handle_v1_group_by_id(self, request: web.Request) -> web.Response:
        """GET /api/{username}/groups/{group_id} — single group object."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        group_id = request.match_info["group_id"]
        groups = self._build_v1_groups()
        if group_id not in groups:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 3,
                            "address": f"/groups/{group_id}",
                            "description": f"resource, /groups/{group_id}, not available",
                        }
                    }
                ],
                status=404,
            )
        return web.json_response(groups[group_id])

    async def _handle_v1_group_put(self, request: web.Request) -> web.Response:
        """PUT /api/{username}/groups/{group_id} — update group (incl. stream start/stop)."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        group_id = request.match_info["group_id"]
        username = request.match_info["username"]
        body = await self._safe_json(request)
        if body is None:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 2,
                            "address": "/",
                            "description": "body contains invalid json",
                        }
                    }
                ]
            )

        # Handle stream activation (TV sends {"stream": {"active": true}} here)
        stream = body.get("stream")
        if stream is not None:
            await self._set_entertainment_active(stream.get("active", False), username)

        # Echo back each field as success
        result = []
        for k, v in body.items():
            if isinstance(v, dict):
                for sk, sv in v.items():
                    result.append({"success": {f"/groups/{group_id}/{k}/{sk}": sv}})
            else:
                result.append({"success": {f"/groups/{group_id}/{k}": v}})
        return web.json_response(result)

    async def _handle_v1_stream(self, request: web.Request) -> web.Response:
        """PUT /api/{username}/groups/{group_id}/stream — v1 entertainment start/stop."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        body = await self._safe_json(request)
        if body is None:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 2,
                            "address": "/",
                            "description": "body contains invalid json",
                        }
                    }
                ]
            )
        # TV sends {"stream": {"active": true}}, but accept flat {"active": true} too
        stream = body.get("stream", body)
        active = stream.get("active", False)
        username = request.match_info["username"]

        await self._set_entertainment_active(active, username)

        return web.json_response([{"success": {"/groups/1/stream/active": active}}])

    async def _handle_v1_catchall(self, request: web.Request) -> web.Response:
        """Catch-all GET for unimplemented v1 resources — avoids 404s."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        return web.json_response({})

    async def _handle_v1_put_catchall(self, request: web.Request) -> web.Response:
        """Catch-all PUT for unimplemented v1 resources — echo success."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        resource = request.match_info.get("resource", "")
        rid = request.match_info.get("id", "")
        param = request.match_info.get("param", "")
        body = await self._safe_json(request)
        if body is None:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 2,
                            "address": "/",
                            "description": "body contains invalid json",
                        }
                    }
                ]
            )
        prefix = f"/{resource}/{rid}"
        if param:
            prefix += f"/{param}"
        result = [{"success": {f"{prefix}/{k}": v}} for k, v in body.items()]
        return web.json_response(result)

    async def _handle_v1_post_catchall(self, request: web.Request) -> web.Response:
        """Catch-all POST for unimplemented v1 resources."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        resource = request.match_info.get("resource", "")
        if resource == "lights":
            return web.json_response([{"success": {"/lights": "Searching for new devices"}}])
        return web.json_response([{"success": {"id": "0"}}])

    async def _handle_v1_delete_catchall(self, request: web.Request) -> web.Response:
        """Catch-all DELETE for unimplemented v1 resources."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        resource = request.match_info.get("resource", "")
        rid = request.match_info.get("id", "")
        # Nothing here can be deleted: say so rather than report a deletion
        # that didn't happen (e.g. a client trying to unpair another one).
        address = f"/{resource}/{rid}"
        return web.json_response(
            [
                {
                    "error": {
                        "type": 3,
                        "address": address,
                        "description": f"resource, {address}, not available",
                    }
                }
            ]
        )

    # ---------------------------------------------------------------------------
    # CLIP v2 (HTTPS only)
    # ---------------------------------------------------------------------------

    def _clip_application_key(self, request: web.Request) -> str | None:
        """Paired username from the ``hue-application-key`` header, or None."""
        key = request.headers.get("hue-application-key", "")
        if key and self._user_store.get_psk(key) is not None:
            return key
        return None

    @staticmethod
    def _clip_response(
        data: list[dict] | None = None, status: int = 200, error: str | None = None
    ) -> web.Response:
        return web.json_response(clip_v2.envelope(data, [error] if error else None), status=status)

    def _clip_resources(self) -> list[dict]:
        return clip_v2.build_resources(
            self._bridge_id,
            self._light_entities,
            active=self._entertainment_active,
            owner=self._entertainment_owner,
        )

    def _clip_log_once(self, request: web.Request, detail: str) -> None:
        """Log what an official client asked for that we don't serve (once per path)."""
        key = f"{request.method} {request.path}"
        if key not in self._clip_logged_paths:
            self._clip_logged_paths.add(key)
            _LOGGER.info("CLIP v2 client asked for %s (%s)", key, detail)

    async def _handle_auth_v1(self, request: web.Request) -> web.Response:
        """GET /auth/v1 — echo the application id (= the username, as diyHue does).

        Whatever the client then uses as its DTLS PSK identity is the
        username, which the DTLS server already resolves.
        """
        username = self._clip_application_key(request)
        if username is None:
            return self._clip_response(status=403, error="unauthorized user")
        return web.json_response({}, headers={"hue-application-id": username})

    async def _handle_clip_all(self, request: web.Request) -> web.Response:
        """GET /clip/v2/resource — every resource."""
        if self._clip_application_key(request) is None:
            return self._clip_response(status=403, error="unauthorized user")
        return self._clip_response(self._clip_resources())

    async def _handle_clip_rtype(self, request: web.Request) -> web.Response:
        """GET /clip/v2/resource/{rtype} — empty list for types we don't model."""
        if self._clip_application_key(request) is None:
            return self._clip_response(status=403, error="unauthorized user")
        rtype = request.match_info["rtype"]
        data = [r for r in self._clip_resources() if r["type"] == rtype]
        if not data:
            self._clip_log_once(request, "not modelled, empty list")
        return self._clip_response(data)

    async def _handle_clip_by_id(self, request: web.Request) -> web.Response:
        """GET /clip/v2/resource/{rtype}/{rid}."""
        if self._clip_application_key(request) is None:
            return self._clip_response(status=403, error="unauthorized user")
        rtype, wanted = request.match_info["rtype"], request.match_info["rid"]
        data = [r for r in self._clip_resources() if r["type"] == rtype and r["id"] == wanted]
        if not data:
            self._clip_log_once(request, "not found")
            return self._clip_response(status=404, error="Not Found")
        return self._clip_response(data)

    async def _handle_clip_ent_put(self, request: web.Request) -> web.Response:
        """PUT …/entertainment_configuration/{id} — ``{"action": "start"|"stop"}``."""
        username = self._clip_application_key(request)
        if username is None:
            return self._clip_response(status=403, error="unauthorized user")
        config_id = clip_v2.entertainment_configuration_id(self._bridge_id)
        if request.match_info["rid"] != config_id or not self._light_entities:
            return self._clip_response(status=404, error="Not Found")
        body = await self._safe_json(request)
        if not isinstance(body, dict):
            return self._clip_response(status=400, error="body contains invalid json")
        action = body.get("action")
        if action == "start":
            await self._set_entertainment_active(True, username)
        elif action == "stop":
            await self._set_entertainment_active(False, username)
        if set(body) - {"action"}:
            _LOGGER.info("CLIP v2 configuration update ignored: %s", _truncate(body))
        return self._clip_response([{"rid": config_id, "rtype": "entertainment_configuration"}])

    async def _handle_clip_unsupported(self, request: web.Request) -> web.Response:
        """Mutations we don't support (e.g. creating an entertainment area)."""
        if self._clip_application_key(request) is None:
            return self._clip_response(status=403, error="unauthorized user")
        body = await request.text()
        _LOGGER.info(
            "CLIP v2 client request not supported: %s %s %s",
            request.method,
            request.path,
            body[:500],
        )
        return self._clip_response(status=405, error="method not allowed")

    def _publish_configuration_update(self) -> None:
        """Send an ``update`` event for the entertainment configuration's status."""
        if not self._event_queues or not self._light_entities:
            return
        data: dict[str, Any] = {
            "id": clip_v2.entertainment_configuration_id(self._bridge_id),
            "id_v1": "/groups/1",
            "type": "entertainment_configuration",
            "status": "active" if self._entertainment_active else "inactive",
        }
        if self._entertainment_active and self._entertainment_owner:
            data["active_streamer"] = {"rid": self._entertainment_owner, "rtype": "auth_v1"}
        event = {
            "creationtime": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data": [data],
            "id": str(uuid.uuid4()),
            "type": "update",
        }
        for queue in self._event_queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                _LOGGER.debug("Event stream client not reading; event dropped")

    async def _handle_eventstream(self, request: web.Request) -> web.StreamResponse:
        """GET /eventstream/clip/v2 — server-sent events (status changes only)."""
        if self._clip_application_key(request) is None:
            return self._clip_response(status=403, error="unauthorized user")
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
            }
        )
        await resp.prepare(request)
        queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=32)
        self._event_queues.add(queue)
        try:
            await resp.write(b": hi\n\n")
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), EVENTSTREAM_KEEPALIVE)
                except TimeoutError:
                    await resp.write(b": hi\n\n")
                    continue
                if event is None:
                    break
                message = f"id: {int(time.time())}:0\ndata: {json.dumps([event])}\n\n"
                await resp.write(message.encode())
        except ConnectionResetError:
            pass  # client went away
        finally:
            self._event_queues.discard(queue)
        return resp


def _truncate(body: dict) -> str:
    text = json.dumps(body)
    return text if len(text) <= 500 else text[:500] + "…"

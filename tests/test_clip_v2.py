"""CLIP v2 resource model (clip_v2.py) and its HTTPS-only routes (hue_api.py)."""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

_base = Path(__file__).parent.parent / "custom_components" / "hue_entertainment"
_pkg = types.ModuleType("hue_entertainment")
_pkg.__path__ = [str(_base)]  # type: ignore[attr-defined]
sys.modules.setdefault("hue_entertainment", _pkg)


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _base / filename)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_load("hue_entertainment.const", "const.py")
_load("hue_entertainment.user_store", "user_store.py")
clip_v2 = _load("hue_entertainment.clip_v2", "clip_v2.py")
hue_api = _load("hue_entertainment.hue_api", "hue_api.py")

BRIDGE_ID = "001788FFFE123456"
LIGHTS = ["light.left", "light.center", "light.right"]
USER = "a" * 32
KEY = {"hue-application-key": USER}


def _by_type(resources, rtype):
    return [r for r in resources if r["type"] == rtype]


# ---------------------------------------------------------------------------
# Resource model
# ---------------------------------------------------------------------------


class TestModel:
    def test_ids_unique_and_stable(self):
        first = clip_v2.build_resources(BRIDGE_ID, LIGHTS)
        ids = [r["id"] for r in first]
        assert len(ids) == len(set(ids))
        assert ids == [r["id"] for r in clip_v2.build_resources(BRIDGE_ID, LIGHTS)]

    def test_ids_differ_between_bridges(self):
        a = {r["id"] for r in clip_v2.build_resources(BRIDGE_ID, LIGHTS)}
        b = {r["id"] for r in clip_v2.build_resources("001788FFFE654321", LIGHTS)}
        assert not a & b

    def test_every_reference_resolves(self):
        resources = clip_v2.build_resources(BRIDGE_ID, LIGHTS, active=True, owner=USER)
        index = {(r["id"], r["type"]) for r in resources}
        refs: list[dict] = []

        def walk(node, parent_key=None):
            if isinstance(node, dict):
                if set(node) == {"rid", "rtype"} and parent_key != "active_streamer":
                    refs.append(node)
                for k, v in node.items():
                    walk(v, k)
            elif isinstance(node, list):
                for v in node:
                    walk(v, parent_key)

        walk(resources)
        assert refs
        assert all((r["rid"], r["rtype"]) in index for r in refs)

    def test_counts(self):
        resources = clip_v2.build_resources(BRIDGE_ID, LIGHTS)
        counts = {t: len(_by_type(resources, t)) for t in {r["type"] for r in resources}}
        assert counts == {
            "bridge": 1,
            "bridge_home": 1,
            "device": 1 + len(LIGHTS),
            "entertainment": 1 + len(LIGHTS),
            "light": len(LIGHTS),
            "zigbee_connectivity": len(LIGHTS),
            "entertainment_configuration": 1,
        }

    def test_channels_follow_light_order_zero_based(self):
        (cfg,) = _by_type(clip_v2.build_resources(BRIDGE_ID, LIGHTS), "entertainment_configuration")
        assert [c["channel_id"] for c in cfg["channels"]] == [0, 1, 2]
        members = [c["members"][0]["service"]["rid"] for c in cfg["channels"]]
        assert members == [clip_v2.rid(BRIDGE_ID, light, "entertainment") for light in LIGHTS]
        assert [s["rid"] for s in cfg["light_services"]] == [
            clip_v2.rid(BRIDGE_ID, light, "light") for light in LIGHTS
        ]

    def test_positions_match_v1_locations(self):
        server = hue_api.HueAPIServer(BRIDGE_ID, "00:17:88:12:34:56", "10.0.0.2", 80, 3, LIGHTS)
        v1 = server._build_v1_groups()["1"]["locations"]
        (cfg,) = _by_type(clip_v2.build_resources(BRIDGE_ID, LIGHTS), "entertainment_configuration")
        v2 = [[c["position"][k] for k in "xyz"] for c in cfg["channels"]]
        assert v2 == [v1[str(i)] for i in range(1, 4)]

    def test_status_and_active_streamer(self):
        (idle,) = _by_type(
            clip_v2.build_resources(BRIDGE_ID, LIGHTS), "entertainment_configuration"
        )
        assert idle["status"] == "inactive" and "active_streamer" not in idle
        (live,) = _by_type(
            clip_v2.build_resources(BRIDGE_ID, LIGHTS, active=True, owner=USER),
            "entertainment_configuration",
        )
        assert live["status"] == "active"
        assert live["active_streamer"] == {"rid": USER, "rtype": "auth_v1"}

    def test_bridge_identity(self):
        resources = clip_v2.build_resources(BRIDGE_ID, LIGHTS)
        (bridge,) = _by_type(resources, "bridge")
        assert bridge["bridge_id"] == BRIDGE_ID.lower()
        bridge_device = next(r for r in resources if r["id"] == bridge["owner"]["rid"])
        assert bridge_device["product_data"]["model_id"] == "BSB002"
        assert bridge_device["product_data"]["software_version"] == "1.67.1967054020"

    def test_no_lights_no_configuration(self):
        resources = clip_v2.build_resources(BRIDGE_ID, [])
        assert not _by_type(resources, "entertainment_configuration")

    def test_zigbee_mac_matches_v1_uniqueid(self):
        """Hue Essentials requires zigbee_connectivity.mac_address (observed 2026-11-21)."""
        resources = clip_v2.build_resources(BRIDGE_ID, LIGHTS)
        macs = [r["mac_address"] for r in _by_type(resources, "zigbee_connectivity")]
        server = hue_api.HueAPIServer(BRIDGE_ID, "00:17:88:12:34:56", "10.0.0.2", 80, 3, LIGHTS)
        v1 = [server._build_v1_lights()[str(i)]["uniqueid"] for i in range(1, 4)]
        assert [f"{mac}-0b" for mac in macs] == v1

    def test_light_carries_real_bridge_fields(self):
        (light, *_) = _by_type(clip_v2.build_resources(BRIDGE_ID, LIGHTS), "light")
        for field in (
            "service_id",
            "color_temperature",
            "dynamics",
            "alert",
            "signaling",
            "effects",
            "timed_effects",
            "powerup",
            "product_data",
        ):
            assert field in light, field

    def test_light_names_fit_metadata_limit(self):
        long = "light." + "x" * 60
        (light,) = _by_type(clip_v2.build_resources(BRIDGE_ID, [long]), "light")
        assert len(light["metadata"]["name"]) == 32


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _server() -> hue_api.HueAPIServer:
    server = hue_api.HueAPIServer(BRIDGE_ID, "00:17:88:12:34:56", "10.0.0.2", 80, 3, LIGHTS)
    server._user_store.add(USER, "ab" * 16, "Hue Sync#Samsung")
    return server


@pytest_asyncio.fixture
async def clip():
    server = _server()
    app = web.Application()
    server._register_routes(app, clip=True)
    async with TestClient(TestServer(app)) as client:
        yield client, server


@pytest_asyncio.fixture
async def plain():
    """The v1-only app (plain HTTP / Home Assistant's server)."""
    server = _server()
    app = web.Application()
    server._register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.mark.asyncio
class TestRoutes:
    async def test_auth_v1_returns_application_id(self, clip):
        client, _ = clip
        resp = await client.get("/auth/v1", headers=KEY)
        assert resp.status == 200
        assert resp.headers["hue-application-id"] == USER

    @pytest.mark.parametrize("path", ["/auth/v1", "/clip/v2/resource", "/clip/v2/resource/light"])
    @pytest.mark.parametrize("headers", [{}, {"hue-application-key": "unknown"}])
    async def test_unauthorized(self, clip, path, headers):
        client, _ = clip
        resp = await client.get(path, headers=headers)
        assert resp.status == 403
        body = await resp.json()
        assert body == {"errors": [{"description": "unauthorized user"}], "data": []}

    async def test_all_resources(self, clip):
        client, _ = clip
        body = await (await client.get("/clip/v2/resource", headers=KEY)).json()
        assert body["errors"] == []
        assert {r["type"] for r in body["data"]} >= {"bridge", "entertainment_configuration"}

    async def test_filter_by_type(self, clip):
        client, _ = clip
        body = await (await client.get("/clip/v2/resource/light", headers=KEY)).json()
        assert len(body["data"]) == 3 and {r["type"] for r in body["data"]} == {"light"}

    async def test_by_id(self, clip):
        client, _ = clip
        cfg_id = clip_v2.entertainment_configuration_id(BRIDGE_ID)
        resp = await client.get(
            f"/clip/v2/resource/entertainment_configuration/{cfg_id}", headers=KEY
        )
        body = await resp.json()
        assert resp.status == 200 and [r["id"] for r in body["data"]] == [cfg_id]

    async def test_by_id_not_found(self, clip):
        client, _ = clip
        resp = await client.get("/clip/v2/resource/light/nope", headers=KEY)
        assert resp.status == 404
        assert (await resp.json())["errors"] == [{"description": "Not Found"}]

    async def test_unmodelled_type_is_empty_and_logged_once(self, clip, caplog):
        client, _ = clip
        with caplog.at_level(logging.INFO):
            for _ in range(2):
                resp = await client.get("/clip/v2/resource/scene", headers=KEY)
                assert resp.status == 200
                assert await resp.json() == {"errors": [], "data": []}
        logged = [
            r
            for r in caplog.records
            if r.name.endswith("hue_api") and "/clip/v2/resource/scene" in r.getMessage()
        ]
        assert len(logged) == 1

    async def test_status_reflects_streaming(self, clip):
        client, server = clip
        await server._set_entertainment_active(True, USER)
        body = await (
            await client.get("/clip/v2/resource/entertainment_configuration", headers=KEY)
        ).json()
        assert body["data"][0]["status"] == "active"
        assert body["data"][0]["active_streamer"]["rid"] == USER

    async def test_pairing_with_trailing_slash(self, clip):
        """Hue Sync for macOS pairs with POST /api/ (observed 2026-11-21)."""
        client, server = clip
        server.set_link_button(True)
        resp = await client.post(
            "/api/", json={"devicetype": "HueSync#mac", "generateclientkey": True}
        )
        success = (await resp.json())[0]["success"]
        assert set(success) == {"username", "clientkey"}
        assert server.get_user_psk(success["username"]) == success["clientkey"]

    async def test_v1_paths_accept_trailing_slash(self, clip):
        """Hue Sync for macOS also asks for GET /api/config/ (observed 2026-11-21)."""
        client, _ = clip
        resp = await client.get("/api/config/")
        assert resp.status == 200 and (await resp.json())["bridgeid"] == BRIDGE_ID
        resp = await client.get(f"/api/{USER}/lights/")
        assert resp.status == 200 and len(await resp.json()) == 3

    async def test_clip_paths_accept_trailing_slash(self, clip):
        """Hue Essentials opens GET /eventstream/clip/v2/ (observed 2026-11-21)."""
        client, _ = clip
        assert (await client.get("/clip/v2/resource/", headers=KEY)).status == 200
        resp = await client.get("/eventstream/clip/v2/", headers=KEY)
        assert resp.status == 200 and await resp.content.readline() == b": hi\n"
        resp.close()

    async def test_plain_http_keeps_strict_paths(self, plain):
        assert (await plain.post("/api/", json={"devicetype": "x"})).status == 404

    async def test_v1_still_served_alongside(self, clip):
        client, _ = clip
        resp = await client.get("/api/config")
        assert resp.status == 200 and (await resp.json())["bridgeid"] == BRIDGE_ID

    async def test_clip_not_on_plain_http(self, plain):
        for path in ("/auth/v1", "/clip/v2/resource"):
            assert (await plain.get(path, headers=KEY)).status == 404


# ---------------------------------------------------------------------------
# Start / stop and event stream
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import json  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

CONFIG_ID = clip_v2.entertainment_configuration_id(BRIDGE_ID)
CONFIG_PATH = f"/clip/v2/resource/entertainment_configuration/{CONFIG_ID}"


async def _read_event(resp) -> dict:
    """Next ``data:`` message from an SSE response (skips comments)."""
    buf = ""
    while True:
        chunk = await asyncio.wait_for(resp.content.readline(), 2)
        line = chunk.decode()
        if line.startswith("data: "):
            buf = line[len("data: ") :]
        elif line == "\n" and buf:
            return json.loads(buf)[0]


@pytest.mark.asyncio
class TestStartStop:
    async def test_start_and_stop_drive_callbacks(self, clip):
        client, server = clip
        on_start, on_stop = AsyncMock(), AsyncMock()
        server.set_entertainment_callbacks(on_start, on_stop)

        resp = await client.put(CONFIG_PATH, json={"action": "start"}, headers=KEY)
        assert resp.status == 200
        assert await resp.json() == {
            "errors": [],
            "data": [{"rid": CONFIG_ID, "rtype": "entertainment_configuration"}],
        }
        on_start.assert_awaited_once_with(USER)
        assert server.entertainment_active and server.entertainment_owner == USER

        await client.put(CONFIG_PATH, json={"action": "stop"}, headers=KEY)
        on_stop.assert_awaited_once()
        assert not server.entertainment_active

    async def test_unknown_configuration(self, clip):
        client, _ = clip
        resp = await client.put(
            "/clip/v2/resource/entertainment_configuration/nope",
            json={"action": "start"},
            headers=KEY,
        )
        assert resp.status == 404

    async def test_invalid_json(self, clip):
        client, _ = clip
        resp = await client.put(CONFIG_PATH, data=b"{nope", headers=KEY)
        assert resp.status == 400

    async def test_unauthorized(self, clip):
        client, server = clip
        resp = await client.put(CONFIG_PATH, json={"action": "start"})
        assert resp.status == 403 and not server.entertainment_active

    async def test_other_fields_accepted_and_logged(self, clip, caplog):
        client, server = clip
        with caplog.at_level(logging.INFO):
            resp = await client.put(CONFIG_PATH, json={"metadata": {"name": "x"}}, headers=KEY)
        assert resp.status == 200 and not server.entertainment_active
        assert any("configuration update ignored" in r.getMessage() for r in caplog.records)

    async def test_unsupported_mutation_is_405_and_logged(self, clip, caplog):
        client, _ = clip
        with caplog.at_level(logging.INFO):
            resp = await client.post(
                "/clip/v2/resource/entertainment_configuration",
                json={"metadata": {"name": "Living room"}},
                headers=KEY,
            )
        assert resp.status == 405
        assert (await resp.json())["errors"] == [{"description": "method not allowed"}]
        assert any("Living room" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
class TestEventStream:
    async def test_unauthorized(self, clip):
        client, _ = clip
        assert (await client.get("/eventstream/clip/v2")).status == 403

    async def test_greets_then_reports_start_and_stop(self, clip):
        client, server = clip
        resp = await client.get("/eventstream/clip/v2", headers=KEY)
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        assert await resp.content.readline() == b": hi\n"

        await client.put(CONFIG_PATH, json={"action": "start"}, headers=KEY)
        event = await _read_event(resp)
        assert event["type"] == "update"
        assert event["data"] == [
            {
                "id": CONFIG_ID,
                "id_v1": "/groups/1",
                "type": "entertainment_configuration",
                "status": "active",
                "active_streamer": {"rid": USER, "rtype": "auth_v1"},
            }
        ]

        await client.put(CONFIG_PATH, json={"action": "stop"}, headers=KEY)
        assert (await _read_event(resp))["data"][0]["status"] == "inactive"
        resp.close()

    async def test_watchdog_teardown_reports_inactive(self, clip):
        """clear_entertainment (watchdog / release) must reach the stream too."""
        client, server = clip
        await server._set_entertainment_active(True, USER)
        resp = await client.get("/eventstream/clip/v2", headers=KEY)
        await resp.content.readline()
        server.clear_entertainment()
        assert (await _read_event(resp))["data"][0]["status"] == "inactive"
        resp.close()

    async def test_v1_stream_start_also_reported(self, clip):
        client, server = clip
        resp = await client.get("/eventstream/clip/v2", headers=KEY)
        await resp.content.readline()
        await client.put(f"/api/{USER}/groups/1", json={"stream": {"active": True}})
        assert (await _read_event(resp))["data"][0]["status"] == "active"
        resp.close()

    async def test_keepalive(self, clip):
        client, _ = clip
        with patch.object(hue_api, "EVENTSTREAM_KEEPALIVE", 0.05):
            resp = await client.get("/eventstream/clip/v2", headers=KEY)
            assert await resp.content.readline() == b": hi\n"
            await resp.content.readline()  # blank line
            assert await asyncio.wait_for(resp.content.readline(), 1) == b": hi\n"
        resp.close()

    async def test_server_stop_ends_streams(self, clip):
        client, server = clip
        resp = await client.get("/eventstream/clip/v2", headers=KEY)
        await resp.content.readline()
        await server.async_stop()
        rest = await asyncio.wait_for(resp.content.read(), 2)
        assert b"data:" not in rest
        assert not server._event_queues

    async def test_stop_with_a_stalled_client(self, clip):
        """A client that never reads fills its queue; stopping must still work."""
        client, server = clip
        resp = await client.get("/eventstream/clip/v2", headers=KEY)
        await resp.content.readline()
        (queue,) = server._event_queues
        for _ in range(queue.maxsize):
            queue.put_nowait({"type": "update"})
        await server.async_stop()  # used to raise QueueFull
        assert queue.get_nowait() is None
        resp.close()

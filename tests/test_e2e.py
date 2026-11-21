"""End-to-end integration test: pair → DTLS stream → frames received.

HTTP pairing uses aiohttp TestClient (no real port).
DTLS uses a real DTLSPSKServer + openssl s_client subprocess.

Run manually (requires openssl binary):
    pytest tests/test_e2e.py -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import struct
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from conftest import free_udp_port
from hue_entertainment.dtls_psk import DTLSPSKServer

# ---------------------------------------------------------------------------
# Bootstrap: load hue_entertainment modules without a real HA install
# ---------------------------------------------------------------------------

_base = Path(__file__).parent.parent / "custom_components" / "hue_entertainment"

_pkg_stub = types.ModuleType("hue_entertainment")
_pkg_stub.__path__ = [str(_base)]  # type: ignore[attr-defined]
_pkg_stub.__package__ = "hue_entertainment"
sys.modules.setdefault("hue_entertainment", _pkg_stub)


def _load(name: str, filename: str):
    path = _base / filename
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_load("hue_entertainment.const", "const.py")
_load("hue_entertainment.user_store", "user_store.py")
_hue_api = _load("hue_entertainment.hue_api", "hue_api.py")

HueAPIServer = _hue_api.HueAPIServer

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_BRIDGE_ID = "001788FFFE654321"
_MAC = "00:17:88:65:43:21"
_CONFIG_ID = "e2e-config-uuid-0001"
_LIGHTS = ["light.a", "light.b", "light.c"]


# ---------------------------------------------------------------------------
# Helper: build a HueStream v2 frame
# ---------------------------------------------------------------------------


def build_huestream_frame(config_id: str, channels: list[tuple[int, int, int, int]]) -> bytes:
    """Build a minimal HueStream v2 frame.

    channels: list of (channel_id, r, g, b) with 16-bit values.
    """
    header = bytearray()
    header.extend(b"HueStream")  # 9 bytes
    header.extend(b"\x02\x00")  # API version 2.0
    header.append(0x01)  # sequence number
    header.extend(b"\x00\x00")  # reserved
    header.append(0x00)  # colorspace: RGB
    header.append(0x00)  # reserved
    header.extend(config_id.encode("ascii")[:36].ljust(36, b"\x00"))  # 36-byte UUID

    for channel_id, r, g, b in channels:
        header.append(channel_id)
        header.extend(struct.pack(">HHH", r, g, b))

    return bytes(header)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def bridge():
    """Wire HueAPIServer (TestClient) + DTLSPSKServer (real port)."""
    frames: list[bytes] = []
    frame_event = asyncio.Event()

    def on_frame(data: bytes) -> None:
        frames.append(data)
        frame_event.set()

    api_server = HueAPIServer(
        bridge_id=_BRIDGE_ID,
        mac=_MAC,
        host_ip="127.0.0.1",
        http_port=0,
        channel_count=len(_LIGHTS),
        light_entities=_LIGHTS,
    )
    api_server.set_link_button(True)

    def psk_lookup(identity: str) -> bytes | None:
        hex_key = api_server.get_user_psk(identity)
        return bytes.fromhex(hex_key) if hex_key else None

    loop = asyncio.get_running_loop()
    dtls_server = DTLSPSKServer(
        host="127.0.0.1",
        port=free_udp_port(),
        psk_callback=psk_lookup,
        frame_callback=on_frame,
        loop=loop,
    )

    app = web.Application()
    api_server._register_routes(app)

    await dtls_server.async_start()
    try:
        async with TestClient(TestServer(app)) as client:
            yield client, api_server, dtls_server, frames, frame_event
    finally:
        await dtls_server.async_stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestE2EFlow:
    @pytest.mark.asyncio
    async def test_full_tv_flow(self, bridge):
        """Pair → start entertainment → DTLS stream → frames arrive."""
        client, api_server, dtls_server, frames, frame_event = bridge

        # Pair
        resp = await client.post("/api", json={"devicetype": "tv#test", "generateclientkey": True})
        body = await resp.json()
        username = body[0]["success"]["username"]
        clientkey = body[0]["success"]["clientkey"]

        # Start entertainment (v1 group PUT)
        put_resp = await client.put(
            f"/api/{username}/groups/1",
            json={"stream": {"active": True}},
        )
        assert put_resp.status == 200

        # Connect via DTLS and send HueStream frames
        proc = subprocess.Popen(
            [
                "openssl",
                "s_client",
                "-dtls",
                "-psk",
                clientkey,
                "-psk_identity",
                username,
                "-connect",
                f"127.0.0.1:{dtls_server._port}",
                "-quiet",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            frame = build_huestream_frame(
                _CONFIG_ID, [(0, 0xFFFF, 0, 0), (1, 0, 0xFFFF, 0), (2, 0, 0, 0xFFFF)]
            )
            for _ in range(5):
                proc.stdin.write(frame)
                proc.stdin.flush()
                time.sleep(0.05)

            await asyncio.wait_for(frame_event.wait(), timeout=5.0)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

        assert len(frames) >= 1
        assert frames[0].startswith(b"HueStream")

        # Stop entertainment (v1 group PUT)
        await client.put(
            f"/api/{username}/groups/1",
            json={"stream": {"active": False}},
        )

    @pytest.mark.asyncio
    async def test_wrong_psk_rejected(self, bridge):
        """Client with correct identity but wrong PSK gets no frames."""
        client, api_server, dtls_server, frames, frame_event = bridge

        # Pair (server now knows the correct clientkey)
        resp = await client.post("/api", json={"devicetype": "tv#test", "generateclientkey": True})
        body = await resp.json()
        username = body[0]["success"]["username"]
        wrong_key = "0000000000000000000000000000000f"  # 16 bytes, wrong value

        proc = subprocess.Popen(
            [
                "openssl",
                "s_client",
                "-dtls",
                "-psk",
                wrong_key,
                "-psk_identity",
                username,
                "-connect",
                f"127.0.0.1:{dtls_server._port}",
                "-quiet",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            proc.stdin.write(b"bad data")
            proc.stdin.flush()
            await asyncio.sleep(1.0)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

        assert len(frames) == 0, "Frames received despite wrong PSK"

    @pytest.mark.asyncio
    async def test_unpaired_identity_rejected(self, bridge):
        """Client with an unknown identity gets no frames."""
        client, api_server, dtls_server, frames, frame_event = bridge

        unknown_user = "never-paired-identity"
        some_key = "aabbccddeeff00112233445566778899"

        proc = subprocess.Popen(
            [
                "openssl",
                "s_client",
                "-dtls",
                "-psk",
                some_key,
                "-psk_identity",
                unknown_user,
                "-connect",
                f"127.0.0.1:{dtls_server._port}",
                "-quiet",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            proc.stdin.write(b"bad data")
            proc.stdin.flush()
            await asyncio.sleep(1.0)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

        assert len(frames) == 0, "Frames received for unknown identity"


# ---------------------------------------------------------------------------
# Hue Sync app flow: CLIP v2 over real HTTPS, then DTLS with HueStream v2
# ---------------------------------------------------------------------------

import json  # noqa: E402
import socket  # noqa: E402
import ssl  # noqa: E402

import aiohttp  # noqa: E402

_certificate = _load("hue_entertainment.certificate", "certificate.py")
_clip_v2 = sys.modules["hue_entertainment.clip_v2"]


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _official_client_tls() -> ssl.SSLContext:
    """TLS 1.2 with the one suite a real bridge offers; trust is the client's problem."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("ECDHE-ECDSA-AES128-GCM-SHA256")
    return ctx


@pytest_asyncio.fixture
async def https_bridge(tmp_path):
    frames: list[bytes] = []
    frame_event = asyncio.Event()

    def on_frame(data: bytes) -> None:
        frames.append(data)
        frame_event.set()

    pem = _certificate.ensure_certificate(tmp_path / "bridge.pem", _BRIDGE_ID)
    port = _free_tcp_port()
    api_server = HueAPIServer(
        bridge_id=_BRIDGE_ID,
        mac=_MAC,
        host_ip="127.0.0.1",
        http_port=0,
        channel_count=len(_LIGHTS),
        light_entities=_LIGHTS,
        bind_ip="127.0.0.1",
        ssl_context=_certificate.create_ssl_context(pem),
        https_port=port,
    )
    api_server.set_link_button(True)

    def psk_lookup(identity: str) -> bytes | None:
        hex_key = api_server.get_user_psk(identity)
        return bytes.fromhex(hex_key) if hex_key else None

    dtls_server = DTLSPSKServer(
        host="127.0.0.1",
        port=free_udp_port(),
        psk_callback=psk_lookup,
        frame_callback=on_frame,
        loop=asyncio.get_running_loop(),
    )
    await api_server.async_start_https()
    await dtls_server.async_start()
    connector = aiohttp.TCPConnector(ssl=_official_client_tls())
    try:
        async with aiohttp.ClientSession(
            base_url=f"https://127.0.0.1:{port}", connector=connector
        ) as session:
            yield session, api_server, dtls_server, frames, frame_event
    finally:
        await dtls_server.async_stop()
        await api_server.async_stop()


async def _next_event(resp) -> dict:
    data = ""
    while True:
        line = (await asyncio.wait_for(resp.content.readline(), 5)).decode()
        if line.startswith("data: "):
            data = line[len("data: ") :]
        elif line == "\n" and data:
            return json.loads(data)[0]


class TestHueSyncFlow:
    @pytest.mark.asyncio
    async def test_pair_start_and_stream_over_https(self, https_bridge):
        session, api_server, dtls_server, frames, frame_event = https_bridge

        # Pair over HTTPS, exactly like v1
        async with session.post(
            "/api", json={"devicetype": "Hue Sync#Samsung", "generateclientkey": True}
        ) as resp:
            success = (await resp.json())[0]["success"]
        app_key, clientkey = success["username"], success["clientkey"]
        headers = {"hue-application-key": app_key}

        async with session.get("/auth/v1", headers=headers) as resp:
            app_id = resp.headers["hue-application-id"]

        async with session.get(
            "/clip/v2/resource/entertainment_configuration", headers=headers
        ) as resp:
            (config,) = (await resp.json())["data"]
        assert [c["channel_id"] for c in config["channels"]] == [0, 1, 2]

        events = await session.get("/eventstream/clip/v2", headers=headers)
        assert await events.content.readline() == b": hi\n"

        async with session.put(
            f"/clip/v2/resource/entertainment_configuration/{config['id']}",
            json={"action": "start"},
            headers=headers,
        ) as resp:
            assert resp.status == 200
        event = await _next_event(events)
        assert event["data"][0]["status"] == "active"
        assert event["data"][0]["active_streamer"]["rid"] == app_id

        # DTLS with the application id as PSK identity, HueStream v2 frames
        proc = subprocess.Popen(
            [
                "openssl",
                "s_client",
                "-dtls",
                "-psk",
                clientkey,
                "-psk_identity",
                app_id,
                "-connect",
                f"127.0.0.1:{dtls_server._port}",
                "-quiet",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            frame = build_huestream_frame(config["id"], [(0, 0xFFFF, 0, 0)])
            for _ in range(5):
                proc.stdin.write(frame)
                proc.stdin.flush()
                time.sleep(0.05)
            await asyncio.wait_for(frame_event.wait(), timeout=5.0)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

        assert frames[0][9] == 0x02  # HueStream v2
        assert frames[0][16:52].decode() == config["id"]
        assert frames[0][52] == 0  # channel 0 = first selected light

        async with session.put(
            f"/clip/v2/resource/entertainment_configuration/{config['id']}",
            json={"action": "stop"},
            headers=headers,
        ) as resp:
            assert resp.status == 200
        assert (await _next_event(events))["data"][0]["status"] == "inactive"
        events.close()

    @pytest.mark.asyncio
    async def test_tls_presents_bridge_certificate(self, https_bridge):
        _, api_server, *_ = https_bridge
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", api_server._https_port, ssl=_official_client_tls()
        )
        try:
            tls = writer.get_extra_info("ssl_object")
            assert tls.version() == "TLSv1.2"
            assert tls.cipher()[0] == "ECDHE-ECDSA-AES128-GCM-SHA256"
            der = tls.getpeercert(binary_form=True)
        finally:
            writer.close()
            await writer.wait_closed()
        from cryptography import x509  # noqa: PLC0415

        cert = x509.load_der_x509_certificate(der)
        assert cert.subject.rfc4514_string() == f"CN={_BRIDGE_ID.lower()},O=Philips Hue,C=NL"

    @pytest.mark.asyncio
    async def test_https_refuses_proxied_requests(self, https_bridge):
        """LAN-only like the HTTP server: a forwarded request gets a 404."""
        session, *_ = https_bridge
        async with session.get("/api/config") as resp:
            assert resp.status == 200
        async with session.get("/api/config", headers={"X-Forwarded-For": "1.1.1.1"}) as resp:
            assert resp.status == 404

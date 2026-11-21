"""SSDP responder (ssdp.py): real UDP exchange on a random port."""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
import types
from pathlib import Path

import pytest

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
ssdp = _load("hue_entertainment.ssdp", "ssdp.py")

MAC = "00:17:88:12:34:56"
UUID = "uuid:2f402f80-da50-11e1-9b23-001788123456"


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _msearch(st: str, man: str = '"ssdp:discover"') -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: 239.255.255.250:1900\r\nMAN: {man}\r\nMX: 0\r\nST: {st}\r\n\r\n"
    ).encode()


@pytest.fixture
async def responder():
    port = _free_udp_port()
    r = ssdp.SsdpResponder("127.0.0.1", 80, "001788FFFE123456", MAC, port=port)
    assert await r.async_start()
    yield port
    await r.async_stop()


async def _ask(port: int, payload: bytes, expect: int, timeout: float = 1.0) -> list[str]:
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.setblocking(False)
        client.bind(("127.0.0.1", 0))
        client.sendto(payload, ("127.0.0.1", port))
        replies: list[str] = []
        deadline = loop.time() + timeout
        while loop.time() < deadline and len(replies) < max(expect, 1):
            try:
                data = await asyncio.wait_for(loop.sock_recv(client, 2048), 0.3)
            except TimeoutError:
                continue
            replies.append(data.decode())
        return replies


def _header(reply: str, name: str) -> str:
    for line in reply.split("\r\n"):
        key, _, value = line.partition(":")
        if key.strip().lower() == name.lower():
            return value.strip()
    raise KeyError(name)


@pytest.mark.asyncio
async def test_ssdp_all_gets_three_replies(responder):
    replies = await _ask(responder, _msearch("ssdp:all"), expect=3)
    assert len(replies) == 3
    assert {_header(r, "ST") for r in replies} == {
        "upnp:rootdevice",
        UUID,
        "urn:schemas-upnp-org:device:basic:1",
    }
    for reply in replies:
        assert reply.startswith("HTTP/1.1 200 OK")
        assert _header(reply, "LOCATION") == "http://127.0.0.1:80/description.xml"
        assert _header(reply, "hue-bridgeid") == "001788FFFE123456"
        assert "IpBridge/" in _header(reply, "SERVER")


@pytest.mark.asyncio
async def test_root_device_search(responder):
    (reply,) = await _ask(responder, _msearch("upnp:rootdevice"), expect=1)
    assert _header(reply, "USN") == f"{UUID}::upnp:rootdevice"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        _msearch("urn:schemas-upnp-org:device:MediaRenderer:1"),
        _msearch("ssdp:all", man='"ssdp:other"'),
        b"NOTIFY * HTTP/1.1\r\nNT: upnp:rootdevice\r\n\r\n",
        b"\xff\xfe garbage",
    ],
)
async def test_ignores_other_traffic(responder, payload):
    assert await _ask(responder, payload, expect=0, timeout=0.5) == []

"""SSDP (UPnP) discovery for the bridge: answers M-SEARCH, announces itself.

Official Hue apps find bridges through Signify's cloud, mDNS or SSDP; diyHue
(reported to work with the Samsung Hue Sync app) does both mDNS and SSDP, so
we can't tell which one the app relies on. Only started with Hue Sync app
support (the Ambilight TV uses mDNS).

UDP 1900 is shared with Home Assistant's own SSDP integration: the socket is
bound with SO_REUSEADDR / SO_REUSEPORT so both receive the multicast queries.
If the port still can't be had, SSDP simply stays off.
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
from typing import Any

from .const import BRIDGE_API_VERSION

_LOGGER = logging.getLogger(__name__)

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
NOTIFY_INTERVAL = 60.0  # seconds between ssdp:alive announcements
MAX_AGE = 100
_ST_ALL = "ssdp:all"
_ST_ROOT = "upnp:rootdevice"
_ST_BASIC = "urn:schemas-upnp-org:device:basic:1"


class SsdpResponder(asyncio.DatagramProtocol):
    """Answer SSDP M-SEARCH queries for the bridge and announce it periodically."""

    def __init__(
        self,
        host_ip: str,
        http_port: int,
        bridge_id: str,
        mac: str,
        port: int = SSDP_PORT,
    ) -> None:
        self._host_ip = host_ip
        self._port = port
        serial = mac.replace(":", "").lower()
        # Same UDN as /description.xml
        self._uuid = f"uuid:2f402f80-da50-11e1-9b23-{serial}"
        self._location = f"http://{host_ip}:{http_port}/description.xml"
        self._bridge_id = bridge_id.upper()
        self._transport: asyncio.DatagramTransport | None = None
        self._notify_task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------

    async def async_start(self) -> bool:
        """Bind and start answering; False (logged) if the port can't be shared."""
        loop = asyncio.get_running_loop()
        try:
            sock = self._make_socket()
        except OSError as err:
            _LOGGER.warning("SSDP not available on UDP %d: %s", self._port, err)
            return False
        transport, _ = await loop.create_datagram_endpoint(lambda: self, sock=sock)
        self._transport = transport  # type: ignore[assignment]
        self._notify_task = loop.create_task(self._notify_loop())
        _LOGGER.info("SSDP: answering for %s on UDP %d", self._location, self._port)
        return True

    async def async_stop(self) -> None:
        """Stop answering and announcing."""
        if self._notify_task is not None:
            self._notify_task.cancel()
            self._notify_task = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _make_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", self._port))
            try:
                mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton(self._host_ip)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self._host_ip)
                )
            except OSError as err:
                # Still answers queries sent to it directly
                _LOGGER.debug("SSDP: could not join the multicast group: %s", err)
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        return sock

    # -- protocol ----------------------------------------------------------

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        """Answer an M-SEARCH for us (after a short random delay, as UPnP asks)."""
        headers = _parse_msearch(data)
        if headers is None:
            return
        targets = self._targets(headers.get("st", ""))
        if not targets:
            return
        try:
            mx = max(0, min(int(headers.get("mx", "1")), 5))
        except ValueError:
            mx = 1
        delay = random.uniform(0, min(mx, 1)) if mx else 0
        asyncio.get_running_loop().call_later(delay, self._respond, targets, addr)

    def _targets(self, st: str) -> list[tuple[str, str]]:
        """(ST/NT, USN) pairs matching a search target."""
        all_targets = [
            (_ST_ROOT, f"{self._uuid}::{_ST_ROOT}"),
            (self._uuid, self._uuid),
            (_ST_BASIC, self._uuid),
        ]
        if st == _ST_ALL:
            return all_targets
        return [t for t in all_targets if t[0] == st]

    def _respond(self, targets: list[tuple[str, str]], addr: tuple[str | Any, int]) -> None:
        if self._transport is None:
            return
        for st, usn in targets:
            self._transport.sendto(self._message("HTTP/1.1 200 OK", "ST", st, usn), addr)

    async def _notify_loop(self) -> None:
        while True:
            if self._transport is not None:
                for nt, usn in self._targets(_ST_ALL):
                    self._transport.sendto(
                        self._message("NOTIFY * HTTP/1.1", "NT", nt, usn, notify=True),
                        (SSDP_ADDR, SSDP_PORT),
                    )
            await asyncio.sleep(NOTIFY_INTERVAL)

    def _message(self, first: str, kind: str, target: str, usn: str, notify: bool = False) -> bytes:
        lines = [
            first,
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}",
            "EXT:",
            f"CACHE-CONTROL: max-age={MAX_AGE}",
            f"LOCATION: {self._location}",
            f"SERVER: Linux/3.14.0 UPnP/1.0 IpBridge/{BRIDGE_API_VERSION}",
            f"hue-bridgeid: {self._bridge_id}",
            f"{kind}: {target}",
            f"USN: {usn}",
        ]
        if notify:
            lines.insert(5, "NTS: ssdp:alive")
        return ("\r\n".join(lines) + "\r\n\r\n").encode()


def _parse_msearch(data: bytes) -> dict[str, str] | None:
    """Headers of an ssdp:discover M-SEARCH (lower-case names), or None."""
    lines = data.decode("utf-8", errors="replace").split("\r\n")
    if not lines or not lines[0].upper().startswith("M-SEARCH * HTTP/1.1"):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip().strip('"')
    if headers.get("man", "").lower() != "ssdp:discover":
        return None
    return headers

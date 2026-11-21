"""Manual test bridge: run the integration's bridge code on this machine, no Home Assistant.

Serves the Hue API on HTTP :80, the opt-in HTTPS + CLIP v2 listener on :443
and DTLS on UDP :2100, advertises the bridge over mDNS, and logs what a real
client does — every request, TLS alerts (how a certificate rejection shows
up), DTLS sessions and decoded HueStream frames. Lights are fake: frames are
printed, not sent anywhere.

Usage (inside `nix develop`, from the repo root):

    python tests/manual_bridge.py [--ip 192.168.1.20] [--lights 3] [--no-https]

Keys while running: `p` + Enter opens the 60 s pairing window (it also opens
at start), `q` + Enter quits. State (bridge id, paired clients, certificate)
lives in ~/.cache/hue-manual-bridge so clients stay paired across runs.
Not collected by pytest.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import logging
import secrets
import socket
import ssl
import struct
import sys
import time
import types
from pathlib import Path

from zeroconf.asyncio import AsyncZeroconf

_BASE = Path(__file__).resolve().parent.parent / "custom_components" / "hue_entertainment"

# Load the integration's modules without its HA-dependent package __init__
_pkg = types.ModuleType("hue_entertainment")
_pkg.__path__ = [str(_BASE)]  # type: ignore[attr-defined]
sys.modules["hue_entertainment"] = _pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"hue_entertainment.{name}", _BASE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = mod  # type: ignore[union-attr]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


const = _load("const")
user_store_mod = _load("user_store")
clip_v2 = _load("clip_v2")
hue_api = _load("hue_api")
certificate = _load("certificate")
discovery = _load("discovery")
from hue_entertainment.dtls_psk import DTLSPSKServer  # noqa: E402

LOG = logging.getLogger("manual_bridge")
DATA_DIR = Path.home() / ".cache" / "hue-manual-bridge"


class JsonStore:
    """Minimal stand-in for HA's Store: the paired users as a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def async_load(self) -> dict | None:
        return json.loads(self._path.read_text()) if self._path.exists() else None

    async def async_save(self, data: dict) -> None:
        self._path.write_text(json.dumps(data, indent=2))


def lan_ip() -> str:
    """The address this machine uses to reach the LAN (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]


def bridge_id(data_dir: Path) -> str:
    path = data_dir / "bridge_id"
    if not path.exists():
        path.write_text("001788FFFE" + secrets.token_hex(3).upper())
    return path.read_text().strip()


def log_tls_alerts(ctx: ssl.SSLContext) -> None:
    """Log TLS alerts a client sends — e.g. unknown_ca / bad_certificate when it
    rejects the bridge certificate (CPython debug hook; skipped if unavailable)."""

    def on_message(conn, direction, version, content_type, msg_type, data):
        if direction != "read" or content_type != ssl._TLSContentType.ALERT:  # type: ignore[attr-defined]
            return
        try:
            name = ssl._TLSAlertType(msg_type).name  # type: ignore[attr-defined]
        except ValueError:
            name = str(msg_type)
        if name == "CLOSE_NOTIFY":
            return  # a normal end of connection
        LOG.warning("TLS alert from client: %s — the client rejected the handshake", name)

    with contextlib.suppress(AttributeError):
        ctx._msg_callback = on_message  # type: ignore[attr-defined]


class FramePrinter:
    """Decode HueStream frames: full dump of the first, then a summary every 2 s."""

    def __init__(self, lights: list[str]) -> None:
        self._lights = lights
        self._count = 0
        self._last: bytes | None = None
        self._first = True

    def reset(self) -> None:
        self._first = True

    def on_frame(self, data: bytes) -> None:
        self._count += 1
        self._last = data
        if self._first:
            self._first = False
            LOG.info("First frame: %s", self.describe(data))

    def describe(self, data: bytes) -> str:
        if not data.startswith(b"HueStream") or len(data) < 16:
            return f"not HueStream ({len(data)} bytes)"
        version, space = data[9], "XY" if data[14] else "RGB"
        if version == 2:
            config, body, size = data[16:52].decode(errors="replace"), data[52:], 7
        else:
            config, body, size = "-", data[16:], 9
        parts = []
        for off in range(0, len(body) - size + 1, size):
            rec = body[off : off + size]
            if version == 2:
                ch, a, b, c = rec[0], *struct.unpack(">HHH", rec[1:7])
                name = self._lights[ch] if ch < len(self._lights) else "?"
            else:
                ch, a, b, c = struct.unpack(">xHHHH", rec)
                name = self._lights[ch - 1] if 0 < ch <= len(self._lights) else "?"
            parts.append(f"{ch}→{name}=({a},{b},{c})")
        return f"v{version} {space} config={config} [{', '.join(parts)}]"

    async def run(self) -> None:
        while True:
            await asyncio.sleep(2)
            if self._count:
                assert self._last is not None
                LOG.info("%.1f fps, last %s", self._count / 2, self.describe(self._last))
                self._count = 0


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ip", default=None, help="address to advertise (default: LAN IP)")
    parser.add_argument("--lights", type=int, default=3, help="number of fake lights")
    parser.add_argument("--no-https", action="store_true", help="v1 only, like an Ambilight TV")
    parser.add_argument("--http-port", type=int, default=80)
    parser.add_argument("--https-port", type=int, default=const.HTTPS_PORT)
    parser.add_argument("--dtls-port", type=int, default=const.DEFAULT_ENTERTAINMENT_PORT)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    logging.getLogger("hue_entertainment").setLevel(logging.DEBUG)  # every request
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)  # duplicate of the above

    args.data_dir.mkdir(parents=True, exist_ok=True)
    ip = args.ip or lan_ip()
    bid = bridge_id(args.data_dir)
    lights = [f"light.fake_{i + 1}" for i in range(args.lights)]

    ssl_context = None
    if not args.no_https:
        pem = certificate.ensure_certificate(args.data_dir / "bridge.pem", bid)
        ssl_context = certificate.create_ssl_context(pem)
        log_tls_alerts(ssl_context)
        LOG.info("Certificate SHA-256 %s", certificate.certificate_fingerprint(pem))

    store = user_store_mod.UserStore(ha_store=JsonStore(args.data_dir / "users.json"))
    await store.async_load()
    server = hue_api.HueAPIServer(
        bridge_id=bid,
        mac=":".join(bid.replace("FFFE", "")[i : i + 2] for i in range(0, 12, 2)).lower(),
        host_ip=ip,
        http_port=args.http_port,
        channel_count=len(lights),
        light_entities=lights,
        user_store=store,
        ssl_context=ssl_context,
        https_port=args.https_port,
    )
    printer = FramePrinter(lights)

    async def on_start(username: str) -> None:
        printer.reset()
        LOG.info(">>> entertainment START by %s…", username[:8])

    async def on_stop() -> None:
        LOG.info("<<< entertainment STOP")

    server.set_entertainment_callbacks(on_start, on_stop)
    server.set_light_command_callback(
        lambda light_id, body: LOG.info("classic-mode PUT light %s: %s", light_id, body)
    )

    def psk_lookup(identity: str) -> bytes | None:
        key = server.get_user_psk(identity)
        LOG.info("DTLS handshake, identity %s… %s", identity[:8], "known" if key else "UNKNOWN")
        return bytes.fromhex(key) if key else None

    loop = asyncio.get_running_loop()
    dtls = DTLSPSKServer("0.0.0.0", args.dtls_port, psk_lookup, printer.on_frame, loop=loop)
    # Only the advertised interface: other interfaces (VPNs, VLANs) just log send errors
    zc = AsyncZeroconf(interfaces=[ip])
    mdns = discovery.HueBridgeDiscovery(bid, ip, args.http_port, zc)

    known = set(store.users)

    async def watch_pairings() -> None:  # the API server persists them itself
        while True:
            await asyncio.sleep(1)
            current = store.users
            for name in set(current) - known:
                LOG.info("Paired: %s… (%s)", name[:8], current[name].get("devicetype"))
                known.add(name)

    await server.async_start()
    if ssl_context is not None:
        await server.async_start_https()
    await dtls.async_start()
    await mdns.async_start()
    server.set_link_button(True)
    LOG.info(
        "Bridge %s at %s — HTTP :%d%s, DTLS :%d, %d fake lights, %d paired client(s). "
        "Pairing window open for %ds; 'p' + Enter reopens it, 'q' + Enter quits.",
        bid,
        ip,
        args.http_port,
        "" if ssl_context is None else f", HTTPS :{args.https_port}",
        args.dtls_port,
        len(lights),
        len(known),
        int(const.LINK_BUTTON_TIMEOUT),
    )

    tasks = [asyncio.create_task(printer.run()), asyncio.create_task(watch_pairings())]
    try:
        if not sys.stdin.isatty():
            await asyncio.Event().wait()  # no terminal: run until interrupted
        while True:
            line = (await loop.run_in_executor(None, sys.stdin.readline)).strip().lower()
            if line == "p":
                server.set_link_button(True)
                LOG.info("Pairing window open for %ds", int(const.LINK_BUTTON_TIMEOUT))
            elif line == "q":
                break
    finally:
        for task in tasks:
            task.cancel()
        await mdns.async_stop()
        await zc.async_close()
        await dtls.async_stop()
        await server.async_stop()
        LOG.info("Stopped at %s", time.strftime("%H:%M:%S"))


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())

"""Tests for the DTLS-PSK server."""

from __future__ import annotations

import asyncio
import subprocess
import time

import pytest
from conftest import free_udp_port
from hue_entertainment.dtls_psk import DTLSPSKServer

TEST_PSK_IDENTITY = "test-user-abc123"
TEST_PSK_KEY = bytes.fromhex("deadbeefcafebabe1234567890abcdef")


def psk_callback(identity: str) -> bytes | None:
    """Test PSK lookup."""
    if identity == TEST_PSK_IDENTITY:
        return TEST_PSK_KEY
    return None


@pytest.fixture
def event_loop():
    """Create an event loop for tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


class TestDTLSPSKServer:
    """Test DTLS server lifecycle and handshake."""

    @pytest.mark.asyncio
    async def test_start_stop(self):
        """Server starts and stops without errors."""
        frames = []
        server = DTLSPSKServer(
            host="127.0.0.1",
            port=free_udp_port(),
            psk_callback=psk_callback,
            frame_callback=frames.append,
        )

        await server.async_start()
        await asyncio.sleep(0.1)
        await server.async_stop()

    @pytest.mark.asyncio
    async def test_stop_is_prompt_while_listening(self):
        """async_stop returns within a few poll intervals and the thread exits."""
        server = DTLSPSKServer(
            host="127.0.0.1",
            port=free_udp_port(),
            psk_callback=psk_callback,
            frame_callback=lambda _f: None,
            read_timeout=30.0,
            poll_interval=0.2,
        )
        await server.async_start()
        await asyncio.sleep(0.3)  # thread is now blocked in DTLSv1_listen
        started = time.monotonic()
        await server.async_stop()
        assert time.monotonic() - started < 2.0
        assert server._thread is not None and not server._thread.is_alive()

    @pytest.mark.asyncio
    async def test_dtls_handshake_and_receive(self):
        """Server completes DTLS handshake and receives data."""
        frames: list[bytes] = []
        frame_event = asyncio.Event()

        def on_frame(data: bytes) -> None:
            frames.append(data)
            if len(frames) >= 3:
                frame_event.set()

        port = free_udp_port()
        server = DTLSPSKServer(
            host="127.0.0.1",
            port=port,
            psk_callback=psk_callback,
            frame_callback=on_frame,
        )

        await server.async_start()

        try:
            # Use openssl s_client as the DTLS client
            proc = subprocess.Popen(
                [
                    "openssl",
                    "s_client",
                    "-dtls",
                    "-psk",
                    TEST_PSK_KEY.hex(),
                    "-psk_identity",
                    TEST_PSK_IDENTITY,
                    "-connect",
                    f"127.0.0.1:{port}",
                    "-quiet",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Send test frames
            test_data = b"HueStream" + b"\x02\x00" + b"\x00" * 41 + b"\x00\xff\xff\x00\x00\x00\x00"
            for _ in range(5):
                proc.stdin.write(test_data)
                proc.stdin.flush()
                time.sleep(0.05)

            # Wait for frames to arrive
            try:
                await asyncio.wait_for(frame_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

            proc.terminate()
            proc.wait(timeout=5)

            assert len(frames) >= 1, f"Expected frames but got {len(frames)}"
            assert frames[0].startswith(b"HueStream")

        finally:
            await server.async_stop()

    @pytest.mark.asyncio
    async def test_wrong_psk_rejected(self):
        """Server rejects a client with the wrong PSK."""
        frames = []
        port = free_udp_port()
        server = DTLSPSKServer(
            host="127.0.0.1",
            port=port,
            psk_callback=psk_callback,
            frame_callback=frames.append,
        )

        await server.async_start()

        try:
            proc = subprocess.Popen(
                [
                    "openssl",
                    "s_client",
                    "-dtls",
                    "-psk",
                    "0000000000000000",  # wrong key
                    "-psk_identity",
                    TEST_PSK_IDENTITY,
                    "-connect",
                    f"127.0.0.1:{port}",
                    "-quiet",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            proc.stdin.write(b"hello")
            proc.stdin.flush()

            await asyncio.sleep(1)

            proc.terminate()
            proc.wait(timeout=5)

            assert len(frames) == 0, "Should not receive frames with wrong PSK"

        finally:
            await server.async_stop()


# ---------------------------------------------------------------------------
# Session control: abort_session, session-end reasons, cipher pinning
# ---------------------------------------------------------------------------

_FRAME = b"HueStream" + b"\x02\x00" + b"\x00" * 41 + b"\x00\xff\xff\x00\x00\x00\x00"


def _client(port: int, quiet: bool = True) -> subprocess.Popen:
    args = [
        "openssl",
        "s_client",
        "-dtls",
        "-psk",
        TEST_PSK_KEY.hex(),
        "-psk_identity",
        TEST_PSK_IDENTITY,
        "-connect",
        f"127.0.0.1:{port}",
    ]
    if quiet:
        args.append("-quiet")
    return subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


async def _send_until(proc: subprocess.Popen, event: asyncio.Event, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not event.is_set() and time.monotonic() < deadline:
        proc.stdin.write(_FRAME)
        proc.stdin.flush()
        await asyncio.sleep(0.05)
    assert event.is_set(), "no frame reached the server"


class TestSessionControl:
    @pytest.fixture
    async def server(self):
        frames: list[bytes] = []
        ends: list[str] = []
        got_frame, ended = asyncio.Event(), asyncio.Event()

        def on_frame(data: bytes) -> None:
            frames.append(data)
            got_frame.set()

        def on_end(reason: str) -> None:
            ends.append(reason)
            ended.set()

        port = free_udp_port()
        server = DTLSPSKServer(
            host="127.0.0.1",
            port=port,
            psk_callback=psk_callback,
            frame_callback=on_frame,
            read_timeout=1.0,
            poll_interval=0.1,
            session_end_callback=on_end,
        )
        await server.async_start()
        try:
            yield server, port, got_frame, ended, ends
        finally:
            await server.async_stop()

    @pytest.mark.asyncio
    async def test_abort_ends_session_and_frees_server(self, server):
        server, port, got_frame, ended, ends = server
        first = _client(port)
        try:
            await _send_until(first, got_frame)
            assert server.in_session
            server.abort_session()
            await asyncio.wait_for(ended.wait(), 3)
            assert ends == ["aborted"] and not server.in_session
        finally:
            first.terminate()
            first.wait(timeout=5)

        # A second client gets in straight away
        got_frame.clear()
        second = _client(port)
        try:
            await _send_until(second, got_frame)
            assert server.in_session
        finally:
            second.terminate()
            second.wait(timeout=5)

    @pytest.mark.asyncio
    async def test_abort_between_sessions_is_a_no_op(self, server):
        server, port, got_frame, ended, ends = server
        server.abort_session()  # nothing to end
        proc = _client(port)
        try:
            await _send_until(proc, got_frame)
            await asyncio.sleep(0.4)  # several polls: the stale abort must not apply
            assert server.in_session and ends == []
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    @pytest.mark.asyncio
    async def test_idle_session_reports_idle(self, server):
        server, port, got_frame, ended, ends = server
        proc = _client(port)
        try:
            await _send_until(proc, got_frame)
            await asyncio.wait_for(ended.wait(), 4)  # read_timeout=1 s
            assert ends == ["idle"]
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    @pytest.mark.asyncio
    async def test_clean_close_reports_closed(self, server):
        server, port, got_frame, ended, ends = server
        proc = _client(port, quiet=False)  # without -quiet, EOF on stdin sends close_notify
        try:
            await _send_until(proc, got_frame)
            proc.stdin.close()
            await asyncio.wait_for(ended.wait(), 4)
            assert ends == ["closed"]
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    @pytest.mark.asyncio
    async def test_cipher_pinned(self):
        """A client offering only another PSK suite can't complete the handshake."""
        frames: list[bytes] = []
        port = free_udp_port()
        server = DTLSPSKServer(
            host="127.0.0.1",
            port=port,
            psk_callback=psk_callback,
            frame_callback=frames.append,
            read_timeout=1.0,
            poll_interval=0.1,
        )
        await server.async_start()
        try:
            proc = subprocess.Popen(
                [
                    "openssl",
                    "s_client",
                    "-dtls1_2",
                    "-cipher",
                    "PSK-AES256-CBC-SHA",
                    "-psk",
                    TEST_PSK_KEY.hex(),
                    "-psk_identity",
                    TEST_PSK_IDENTITY,
                    "-connect",
                    f"127.0.0.1:{port}",
                    "-quiet",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(10):
                if proc.poll() is not None:
                    break
                proc.stdin.write(_FRAME)
                proc.stdin.flush()
                await asyncio.sleep(0.1)
            proc.terminate()
            proc.wait(timeout=5)
            assert frames == []
        finally:
            await server.async_stop()

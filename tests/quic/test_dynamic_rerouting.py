"""QUIC dynamic rerouting test.

Verifies that when the mock daemon's /stream-target response changes, the edge
sidecar tears down the current QUIC connection and establishes a new one to the
new server within the polling interval (≤ 15 s + dial time).

Two server sidecars run on different QUIC ports. The mock daemon's quic_port
field tells the sidecar which one to connect to, so no fixed PEER_QUIC_PORT
env var is needed and both servers can share the same loopback IP.
"""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import (
    MockDaemonServer,
    SidecarProcess,
    _free_port,
    _free_udp_port,
    wait_for_metrics,
)

pytestmark = [pytest.mark.integration_quic]

_EMBD_PAYLOAD = b"EMBD" + b"\xab" * 64  # lossless, never dropped by rate policy


def _send_udp(port: int, data: bytes) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(data, ("127.0.0.1", port))
    finally:
        s.close()


def _recv_one(port: int, timeout: float = 2.0) -> bytes | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port))
    s.settimeout(timeout)
    try:
        data, _ = s.recvfrom(65535)
        return data
    except TimeoutError:
        return None
    finally:
        s.close()


def _drain_until(port: int, predicate, timeout: float = 20.0) -> bool:
    """Read UDP packets on port until predicate(data) is True or timeout."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port))
    s.settimeout(0.5)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                data, _ = s.recvfrom(65535)
                if predicate(data):
                    return True
            except TimeoutError:
                pass
    finally:
        s.close()
    return False


def test_reroute_on_target_change(sidecar_binary, tmp_path):
    """Edge reconnects to server B within the poll interval when target changes."""
    # --- server A ---
    quic_a = _free_udp_port()
    local_a = _free_udp_port()
    metrics_a = _free_port()
    srv_a = SidecarProcess(
        sidecar_binary,
        role="server",
        env={
            "QUIC_BIND": f"127.0.0.1:{quic_a}",
            "LOCAL_UDP_BIND": f"127.0.0.1:{local_a}",
            "METRICS_ADDR": f"127.0.0.1:{metrics_a}",
        },
        log_path=tmp_path / "server_a.log",
    )
    srv_a.start()
    wait_for_metrics(f"http://127.0.0.1:{metrics_a}/metrics")

    # --- server B ---
    quic_b = _free_udp_port()
    local_b = _free_udp_port()
    metrics_b = _free_port()
    srv_b = SidecarProcess(
        sidecar_binary,
        role="server",
        env={
            "QUIC_BIND": f"127.0.0.1:{quic_b}",
            "LOCAL_UDP_BIND": f"127.0.0.1:{local_b}",
            "METRICS_ADDR": f"127.0.0.1:{metrics_b}",
        },
        log_path=tmp_path / "server_b.log",
    )
    srv_b.start()
    wait_for_metrics(f"http://127.0.0.1:{metrics_b}/metrics")

    # --- mock daemon: initially points to server A ---
    mock = MockDaemonServer(target_ip="127.0.0.1", target_port=quic_a)
    daemon_port = mock.start()

    # --- edge sidecar ---
    edge_udp = _free_udp_port()
    edge_metrics = _free_port()
    edge = SidecarProcess(
        sidecar_binary,
        role="edge",
        env={
            "DAEMON_URL": f"http://127.0.0.1:{daemon_port}",
            "LOCAL_UDP_BIND": f"127.0.0.1:{edge_udp}",
            "METRICS_ADDR": f"127.0.0.1:{edge_metrics}",
        },
        log_path=tmp_path / "edge.log",
    )
    edge.start()
    wait_for_metrics(f"http://127.0.0.1:{edge_metrics}/metrics")

    try:
        # Give the edge time to poll, connect to server A, and complete handshake.
        time.sleep(1.5)

        # Simulate server-side inference: one UDP socket per sidecar, bound to an
        # ephemeral port so it does not collide with the sidecar's LOCAL_UDP_BIND.
        infer_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        infer_a.bind(("127.0.0.1", 0))
        infer_a.sendto(b"prime", ("127.0.0.1", local_a))
        infer_a.settimeout(2.0)

        # Verify edge → server A path is live: EMBD from edge arrives at the client.
        _send_udp(edge_udp, _EMBD_PAYLOAD)
        try:
            data, _ = infer_a.recvfrom(65535)
            assert data == _EMBD_PAYLOAD, "server A did not receive initial frame"
        except TimeoutError:
            pytest.fail("server A did not receive frame before reroute")
        finally:
            infer_a.close()

        # Reroute: update mock daemon to point to server B.
        mock.set_target(target_ip="127.0.0.1", target_port=quic_b)

        infer_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        infer_b.bind(("127.0.0.1", 0))
        infer_b.sendto(b"prime", ("127.0.0.1", local_b))
        infer_b.settimeout(0.5)

        # Pump frames continuously; after the sidecar reconnects (≤ 15 s poll +
        # ~1 s dial) they should start arriving at server B.
        deadline = time.monotonic() + 20.0
        got_b = False
        while time.monotonic() < deadline:
            _send_udp(edge_udp, _EMBD_PAYLOAD)
            try:
                data, _ = infer_b.recvfrom(65535)
                if data == _EMBD_PAYLOAD:
                    got_b = True
                    break
            except TimeoutError:
                pass
            time.sleep(0.1)

        infer_b.close()
        assert got_b, "server B never received a frame after reroute (waited 20 s)"

    finally:
        edge.stop()
        srv_a.stop()
        srv_b.stop()
        mock.stop()

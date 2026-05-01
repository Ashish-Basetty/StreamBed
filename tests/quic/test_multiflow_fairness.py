"""Multi-flow fairness: 3 edges -> 1 server, all sending at the same nominal
rate. Each edge runs its own QUIC connection, so QUIC's per-connection
congestion control should give each flow a comparable share. The test
asserts that the slowest edge gets at least `MIN_FAIR_SHARE` of the average,
i.e. no edge is starved.

This is a smoke test, not a saturated-link fairness benchmark — without an
external bottleneck we're really verifying that the multi-conn server
implementation accepts and pumps all peers without head-of-line blocking.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

import pytest


pytestmark = [pytest.mark.integration_quic]


N_PER_EDGE = 200
EDGE_COUNT = 3
SEND_INTERVAL_S = 0.002
PAYLOAD_BYTES = 800

# Slowest edge must get >= MIN_FAIR_SHARE of mean delivered count.
MIN_FAIR_SHARE = 0.70


def _make_chnk(stream_id: bytes, payload: bytes) -> bytes:
    """Build a minimal CHNK datagram: magic(4) + stream_id(16) + idx/total/dlen(12) + payload."""
    assert len(stream_id) == 16
    hdr = struct.pack(">III", 0, 1, len(payload))
    return b"CHNK" + stream_id + hdr + payload


def _bind_udp_listener(port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    s.bind(("127.0.0.1", port))
    s.settimeout(0.5)
    return s


def _send_burst(target_port: int, stream_id: bytes, count: int, interval: float) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = b"x" * PAYLOAD_BYTES
    pkt = _make_chnk(stream_id, payload)
    for _ in range(count):
        s.sendto(pkt, ("127.0.0.1", target_port))
        time.sleep(interval)
    s.close()


def test_three_edges_one_server_fair_share(sidecar_pair_factory):
    pair = sidecar_pair_factory(edge_count=EDGE_COUNT)
    ports = pair["ports"]

    # Bind the local-server-UDP listener before any edge sends traffic. The
    # server sidecar will write into this port; we count by stream_id.
    listener = _bind_udp_listener(ports["server_local_udp"])

    stream_ids = [bytes([i + 1]) * 16 for i in range(EDGE_COUNT)]
    counts = {sid: 0 for sid in stream_ids}

    stop = threading.Event()

    def _drain():
        while not stop.is_set():
            try:
                data, _ = listener.recvfrom(65535)
            except socket.timeout:
                continue
            if len(data) >= 20 and data[:4] == b"CHNK":
                sid = data[4:20]
                if sid in counts:
                    counts[sid] += 1

    drain_thread = threading.Thread(target=_drain, daemon=True)
    drain_thread.start()

    # Let QUIC handshakes complete on all edges.
    time.sleep(2.0)

    senders = []
    for i in range(EDGE_COUNT):
        t = threading.Thread(
            target=_send_burst,
            args=(ports["edges_udp"][i], stream_ids[i], N_PER_EDGE, SEND_INTERVAL_S),
        )
        t.start()
        senders.append(t)
    for t in senders:
        t.join()

    # Drain stragglers.
    time.sleep(2.0)
    stop.set()
    drain_thread.join(timeout=2)
    listener.close()

    delivered = [counts[sid] for sid in stream_ids]
    total = sum(delivered)
    mean = total / EDGE_COUNT if total else 0
    floor = mean * MIN_FAIR_SHARE
    slowest = min(delivered)

    print(f"[Test] delivered per edge: {delivered} (mean={mean:.1f}, floor={floor:.1f})")

    for i, d in enumerate(delivered):
        assert d > 0, (
            f"edge-{i} delivered 0/{N_PER_EDGE} — multi-conn server may not be servicing all peers"
        )

    assert slowest >= floor, (
        f"unfair distribution: slowest={slowest}, floor={floor:.1f}, mean={mean:.1f}, all={delivered}"
    )

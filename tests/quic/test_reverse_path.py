"""Server -> edge reverse-path round trip.

Phase D wiring: when SERVER_REVERSE_UDP_BIND is set on the server sidecar and
LOCAL_RECV_UDP_TARGET is set on the edge sidecar, app data sent into the
server's reverse-path UDP listener should arrive verbatim at the edge's
forward target. The wire transport is the QUIC control stream (same one
pumpFeedback uses), so non-FBCK control msgs ride alongside FBCK without
new framing.

This is the load-bearing test for advisor CSTR (server -> edge advice). The
codec layer (CSTM_RELIABLE chunking + reassembly) is tested separately;
here we just verify a single chunk-sized payload survives round trip.
"""
from __future__ import annotations

import socket
import time

import pytest

pytestmark = [pytest.mark.integration_quic]


def _send_udp(target_port: int, payload: bytes) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(payload, ("127.0.0.1", target_port))
    finally:
        s.close()


def _drain_one(sock: socket.socket, timeout: float = 2.0) -> bytes:
    sock.settimeout(timeout)
    data, _ = sock.recvfrom(65535)
    return data


def test_cstr_roundtrips_server_to_edge(sidecar_pair_factory):
    """A CSTR-tagged payload sent into the server's reverse-path UDP arrives
    byte-for-byte at the edge's local recv target."""
    pair = sidecar_pair_factory(edge_count=1, enable_reverse=True)
    server_recv_port = pair["ports"]["server_reverse_udp"]
    assert server_recv_port is not None
    edge_sock = pair["edge_recv_socks"][0]

    # Give the QUIC handshake a moment so the server has an active peer.
    time.sleep(0.5)

    # Smallest possible "CSTR" frame: 4-byte tag + 76-byte advice payload
    # (8 B timestamp + 17 × float32). Mirrors advisor_server.encode_advice.
    payload = b"CSTR" + (b"\x00" * 76)
    _send_udp(server_recv_port, payload)

    got = _drain_one(edge_sock)
    assert got == payload


def test_unrelated_tag_roundtrips_too(sidecar_pair_factory):
    """The reverse path is tag-agnostic for non-FBCK msgs — anything not FBCK
    is forwarded. Verifies we didn't accidentally hard-code CSTR."""
    pair = sidecar_pair_factory(edge_count=1, enable_reverse=True)
    server_recv_port = pair["ports"]["server_reverse_udp"]
    edge_sock = pair["edge_recv_socks"][0]

    time.sleep(0.5)

    payload = b"ACTN" + b"hello-action"
    _send_udp(server_recv_port, payload)
    got = _drain_one(edge_sock)
    assert got == payload


def test_reverse_path_disabled_by_default(sidecar_pair_factory):
    """Without enable_reverse, no listener is opened on either side and the
    fixture exposes no reverse port. Sanity check that the new flags are
    truly opt-in."""
    pair = sidecar_pair_factory(edge_count=1, enable_reverse=False)
    assert pair["ports"]["server_reverse_udp"] is None
    assert pair["edge_recv_socks"] == [None]

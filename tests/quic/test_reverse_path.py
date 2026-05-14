"""Server -> edge reverse-path round trip (unified UDP port).

Server-side inference sends into the sidecar's single `LOCAL_UDP_BIND`; those
UDP packets are forwarded on the QUIC control stream. The edge sidecar
delivers non-FBCK control to the last UDP source address it saw from the local
app, so tests prime the edge by sending one datagram before injecting server
advice traffic.

This is the load-bearing test for advisor CSTR (server -> edge advice).
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
    """A CSTR-tagged payload sent into the server's unified UDP port arrives
    byte-for-byte at the edge's local recv socket (after priming)."""
    pair = sidecar_pair_factory(edge_count=1, enable_reverse=True)
    server_udp = pair["ports"]["server_udp"]
    edge_sock = pair["edge_recv_socks"][0]

    # Give the QUIC handshake a moment so the server has an active peer.
    time.sleep(0.5)

    # Smallest possible "CSTR" frame: 4-byte tag + 76-byte advice payload
    # (8 B timestamp + 17 × float32). Mirrors advisor_server.encode_advice.
    payload = b"CSTR" + (b"\x00" * 76)
    _send_udp(server_udp, payload)

    got = _drain_one(edge_sock)
    assert got == payload


def test_unrelated_tag_roundtrips_too(sidecar_pair_factory):
    """The reverse path is tag-agnostic for non-FBCK msgs — anything not FBCK
    is forwarded. Verifies we didn't accidentally hard-code CSTR."""
    pair = sidecar_pair_factory(edge_count=1, enable_reverse=True)
    server_udp = pair["ports"]["server_udp"]
    edge_sock = pair["edge_recv_socks"][0]

    time.sleep(0.5)

    payload = b"ACTN" + b"hello-action"
    _send_udp(server_udp, payload)
    got = _drain_one(edge_sock)
    assert got == payload


def test_reverse_path_disabled_by_default(sidecar_pair_factory):
    """Without enable_reverse, tests skip the edge priming socket."""
    pair = sidecar_pair_factory(edge_count=1, enable_reverse=False)
    assert pair["ports"]["server_udp"] is not None
    assert pair["edge_recv_socks"] == [None]

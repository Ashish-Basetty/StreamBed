"""Hypothesis round-trip: any payload prefixed with a known magic round-trips
byte-for-byte through chunk + reassembly.

This is the codec layer (Python). The Go sidecar's magic-prefix dispatch
is covered separately by `go test ./sidecar/internal/common/...`.
"""
from __future__ import annotations

import struct

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st  # noqa: E402

from shared.stream_chunks import CHUNK_MAGIC, CHUNK_SIZE, make_chunks

pytestmark = [pytest.mark.integration_quic]


def _reassemble(chunks):
    parts = {}
    total = None
    sid = None
    for c in chunks:
        assert c[:4] == CHUNK_MAGIC
        s = c[4:20]
        i, n, dl = struct.unpack(">III", c[20:32])
        sid = sid or s
        total = total or n
        assert s == sid
        assert n == total
        parts[i] = c[32 : 32 + dl]
    return b"".join(parts[i] for i in range(total))


@settings(max_examples=200, deadline=None)
@given(payload=st.binary(min_size=0, max_size=CHUNK_SIZE * 20))
def test_chunk_roundtrip_property(payload):
    chunks = make_chunks(payload)
    assert _reassemble(chunks) == payload

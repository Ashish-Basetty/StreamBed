"""Chunking + reassembly correctness, including the >=10-chunk MTU regime."""

import os
import struct

import pytest

from shared.stream_chunks import CHUNK_MAGIC, CHUNK_SIZE, EMBED_MAGIC, make_chunks

pytestmark = pytest.mark.unit


def _reassemble(chunks: list[bytes]) -> bytes:
    parts: dict[int, bytes] = {}
    total = None
    stream_id = None
    for c in chunks:
        assert c[:4] == CHUNK_MAGIC
        sid = c[4:20]
        idx, n, dlen = struct.unpack(">III", c[20:32])
        if stream_id is None:
            stream_id, total = sid, n
        assert sid == stream_id
        assert n == total
        parts[idx] = c[32 : 32 + dlen]
    return b"".join(parts[i] for i in range(total))


def test_chunk_size_is_quic_safe():
    assert CHUNK_SIZE <= 1300, "QUIC datagram path requires sub-MTU chunk size"


def test_single_chunk_roundtrip():
    payload = b"hello world"
    chunks = make_chunks(payload)
    assert len(chunks) == 1
    assert _reassemble(chunks) == payload


def test_large_payload_reassembles_byte_for_byte():
    """Frame producing N>=10 chunks must round-trip exactly."""
    payload = os.urandom(CHUNK_SIZE * 12 + 137)
    chunks = make_chunks(payload)
    assert len(chunks) >= 10
    assert _reassemble(chunks) == payload


def test_chunks_share_stream_id():
    payload = os.urandom(CHUNK_SIZE * 5)
    chunks = make_chunks(payload)
    sids = {c[4:20] for c in chunks}
    assert len(sids) == 1


def test_explicit_stream_id_threaded_through():
    sid = b"X" * 16
    chunks = make_chunks(b"abc" * 1000, stream_id=sid)
    for c in chunks:
        assert c[4:20] == sid


def test_tag_threads_through_to_chunk_prefix():
    """make_chunks(payload, tag=EMBED_MAGIC) prefixes each chunk with EMBD."""
    payload = os.urandom(CHUNK_SIZE * 3 + 50)
    chunks = make_chunks(payload, tag=EMBED_MAGIC)
    assert len(chunks) >= 4
    for c in chunks:
        assert c[:4] == EMBED_MAGIC


def test_invalid_tag_length_rejected():
    with pytest.raises(ValueError):
        make_chunks(b"data", tag=b"TOO_LONG")

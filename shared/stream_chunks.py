"""Lightweight chunk helpers for StreamBed protocol. No cv2, no numpy."""

import math
import os
import struct

CHUNK_MAGIC = b"CHNK"
CHUNK_SIZE = 8000


def make_chunks(payload: bytes, stream_id: bytes | None = None) -> list[bytes]:
    """Split payload into chunks with CHUNK_MAGIC + stream_id + metadata prefix."""
    if stream_id is None:
        stream_id = os.urandom(16)
    n = max(1, math.ceil(len(payload) / CHUNK_SIZE))
    chunks = []
    for i in range(n):
        data = payload[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]
        chunks.append(CHUNK_MAGIC + stream_id + struct.pack(">III", i, n, len(data)) + data)
    return chunks

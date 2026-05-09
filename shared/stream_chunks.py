"""Lightweight chunk helpers for StreamBed protocol. No cv2, no numpy.

Two tag spaces ride the same chunked datagram format:

  CHNK — raw video / lossy bulk data. Eligible for sidecar-policy drop.
  EMBD — embedding / lossless bulk data. Sidecar policy must NOT drop.

Both produce identical wire framing (tag + stream_id + chunk metadata + data);
the receiver picks reassembly buckets independently per stream_id, so the two
streams don't collide even if they ever shared an id (urandom makes that
practically impossible anyway).
"""

import math
import os
import struct

CHUNK_MAGIC = b"CHNK"
EMBED_MAGIC = b"EMBD"
# Sized for QUIC datagram path: path MTU (~1500) - IP/UDP (~28) - QUIC framing (~30) - chunk header (32) leaves headroom.
CHUNK_SIZE = 1200


def make_chunks(
    payload: bytes,
    stream_id: bytes | None = None,
    tag: bytes = CHUNK_MAGIC,
) -> list[bytes]:
    """Split payload into chunks with `tag + stream_id + metadata` prefix.

    `tag` must be exactly 4 bytes. Defaults to CHUNK_MAGIC for backwards
    compatibility with callers that don't yet care about the lossy/lossless
    distinction.
    """
    if len(tag) != 4:
        raise ValueError(f"tag must be exactly 4 bytes, got {len(tag)}")
    if stream_id is None:
        stream_id = os.urandom(16)
    n = max(1, math.ceil(len(payload) / CHUNK_SIZE))
    chunks = []
    for i in range(n):
        data = payload[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]
        chunks.append(tag + stream_id + struct.pack(">III", i, n, len(data)) + data)
    return chunks

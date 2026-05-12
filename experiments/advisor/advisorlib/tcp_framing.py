"""TCP transport for StreamBed chunked, tagged messages.

Same wire format as the QUIC datagram path: each logical message is a
sequence of chunks produced by `shared.stream_chunks.make_chunks`. Each
chunk carries `[tag(4) | stream_id(16) | chunk_index(4) | total_chunks(4)
| data_len(4) | data]`. The TCP byte stream is just the concatenation
of those chunks — no extra envelope.

We deliberately reuse the same chunk header on TCP even though TCP
doesn't need stream_id (no reassembly under reordering / loss). The
benefit: any sniffer, daemon, or sidecar that already parses StreamBed
chunks can parse the host↔edge link with no changes. One wire format
across the system.

Tags reuse `sidecar/internal/common/protocol.go` (Go) and
`shared/stream_chunks.py` (Python):

    CHNK — host-side frames (lossy bulk class)
    ACTN — action / control signal (control class, never dropped)
"""
from __future__ import annotations

import asyncio
import struct

from shared.stream_chunks import (
    CHUNK_MAGIC,
    CHUNK_SIZE,
    make_chunks,
)

# ACTN exists on the Go side (sidecar/internal/common/protocol.go,
# TagACTN) but isn't currently exported as a Python constant. Define
# the literal here — it's a fixed 4-byte ASCII identifier that won't
# change.
ACTN_MAGIC = b"ACTN"

CHUNK_HEADER_SIZE = 32  # tag(4) + stream_id(16) + i(4) + n(4) + data_len(4)


async def read_message(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """Read all chunks of one message off the stream. Returns (tag, payload).

    Validates that every chunk in the sequence shares the same tag and
    stream_id, and that chunk indices are 0..total-1 in order. On TCP
    these constraints fall out of in-order delivery; this is just a
    sanity check that the peer is following the protocol.

    Raises asyncio.IncompleteReadError on disconnect mid-message.
    """
    header = await reader.readexactly(CHUNK_HEADER_SIZE)
    tag = header[:4]
    stream_id = header[4:20]
    idx, total, data_len = struct.unpack(">III", header[20:32])
    if idx != 0:
        raise ValueError(f"expected first chunk index 0, got {idx}")
    parts: list[bytes] = [await reader.readexactly(data_len)]

    for expected in range(1, total):
        h = await reader.readexactly(CHUNK_HEADER_SIZE)
        if h[:4] != tag:
            raise ValueError(f"tag changed mid-message: {h[:4]!r} vs {tag!r}")
        if h[4:20] != stream_id:
            raise ValueError("stream_id changed mid-message")
        i, n, dlen = struct.unpack(">III", h[20:32])
        if i != expected or n != total:
            raise ValueError(f"chunk index/total mismatch at chunk {expected}: "
                             f"got idx={i} total={n}")
        parts.append(await reader.readexactly(dlen))

    return tag, b"".join(parts)


async def write_message(
    writer: asyncio.StreamWriter, tag: bytes, payload: bytes
) -> None:
    """Encode payload with make_chunks (same call the QUIC senders use)
    and write every chunk to the stream in order."""
    for chunk in make_chunks(payload, tag=tag):
        writer.write(chunk)
    await writer.drain()


__all__ = [
    "CHUNK_MAGIC",
    "CHUNK_SIZE",
    "ACTN_MAGIC",
    "CHUNK_HEADER_SIZE",
    "read_message",
    "write_message",
]

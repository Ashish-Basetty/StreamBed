"""TCP transport for StreamBed chunked, tagged messages.

Same wire format as the QUIC datagram path: each logical message is a sequence
of chunks produced by `shared.stream_chunks.make_chunks`. Reusing the chunk
header on TCP lets any StreamBed tool parse host↔inference links with no
changes.
"""
from __future__ import annotations

import asyncio
import struct

from shared.stream_chunks import CHUNK_MAGIC, CHUNK_SIZE, make_chunks  # noqa: F401

ACTN_MAGIC = b"ACTN"
CHUNK_HEADER_SIZE = 32  # tag(4) + stream_id(16) + i(4) + n(4) + data_len(4)


async def read_message(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """Read all chunks of one message. Returns (tag, payload).

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
            raise ValueError(f"chunk index/total mismatch at chunk {expected}: idx={i} total={n}")
        parts.append(await reader.readexactly(dlen))

    return tag, b"".join(parts)


async def write_message(writer: asyncio.StreamWriter, tag: bytes, payload: bytes) -> None:
    """Encode payload with make_chunks and write every chunk to the stream."""
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

"""Unit tests for StreamBedTCPSender and stream_chunks."""

import asyncio
import struct

import numpy as np
import pytest

from shared.interfaces.stream_interface import StreamBedTCPSender, StreamFrame
from shared.stream_chunks import CHUNK_MAGIC, make_chunks

pytestmark = pytest.mark.unit


def make_frame(frame: np.ndarray | None = None, embedding: np.ndarray | None = None) -> StreamFrame:
    return StreamFrame(
        timestamp=123.45,
        frame=frame,
        embedding=embedding,
        model_version="v1",
        source_device_id="device123",
        frame_interleaving_rate=30.0,
    )


@pytest.mark.asyncio
async def test_tcp_sender_send_receive():
    """StreamBedTCPSender sends length-prefixed frames; receiver can parse them."""
    received_payloads = []

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                len_buf = await reader.readexactly(4)
                payload_len = struct.unpack(">I", len_buf)[0]
                payload = await reader.readexactly(payload_len)
                received_payloads.append(payload)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    sender = StreamBedTCPSender()
    await sender.connect("127.0.0.1", port)
    sf = make_frame(
        frame=np.zeros((10, 10, 3), dtype=np.uint8),
        embedding=np.random.rand(8).astype(np.float32),
    )
    ok = await sender.send(sf)
    assert ok
    await sender.close()
    server.close()
    await server.wait_closed()

    await asyncio.sleep(0.05)
    assert len(received_payloads) == 1
    payload = received_payloads[0]
    assert len(payload) >= 32
    frame_len, embedding_len = struct.unpack_from(">II", payload, 24)
    assert frame_len > 0
    assert embedding_len > 0


def test_stream_chunks_make_chunks():
    """make_chunks produces valid chunk format."""
    payload = b"x" * 10000
    chunks = make_chunks(payload)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert chunk[:4] == CHUNK_MAGIC
        assert len(chunk) >= 32
    stream_id = chunks[0][4:20]
    for chunk in chunks:
        assert chunk[4:20] == stream_id

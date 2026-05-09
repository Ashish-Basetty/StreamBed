"""StreamBedUDPSender + StreamBedUDPReceiver round-trip the split.

A StreamFrame carrying both halves becomes two chunked streams on the wire:
one CHNK (frame-only) and one EMBD (embedding-only). The receiver yields two
separate StreamFrames per logical send.
"""
import asyncio

import numpy as np
import pytest

from shared.interfaces.stream_interface import (
    StreamBedUDPReceiver,
    StreamBedUDPSender,
    StreamFrame,
)

pytestmark = pytest.mark.unit


def _make_frame() -> StreamFrame:
    return StreamFrame(
        timestamp=42.0,
        frame=np.full((4, 4, 3), 7, dtype=np.uint8),
        embedding=np.array([0.5, -0.25, 1.0], dtype=np.float32),
        model_version="m",
        source_device_id="dev",
        frame_interleaving_rate=30.0,
    )


@pytest.mark.asyncio
async def test_udp_split_roundtrip():
    receiver = StreamBedUDPReceiver()
    await receiver.listen("127.0.0.1", 0)
    port = receiver.get_local_port()
    assert port is not None

    sender = StreamBedUDPSender()
    await sender.connect("127.0.0.1", port)
    await sender.send(_make_frame())

    # Two separate StreamFrames should land in the queue: one frame-only,
    # one embedding-only. Order is not guaranteed under UDP; collect both.
    f1 = await asyncio.wait_for(receiver.recv_one(), timeout=2.0)
    f2 = await asyncio.wait_for(receiver.recv_one(), timeout=2.0)
    assert f1 is not None and f2 is not None

    halves = sorted(
        [(f.frame is not None, f.embedding is not None) for f in (f1, f2)]
    )
    assert halves == [(False, True), (True, False)], f"unexpected halves: {halves}"

    # Both halves carry the same logical metadata.
    for f in (f1, f2):
        assert f.timestamp == 42.0
        assert f.source_device_id == "dev"

    await sender.close()
    await receiver.stop()

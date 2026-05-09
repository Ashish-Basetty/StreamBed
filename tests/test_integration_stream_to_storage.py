import asyncio

import numpy as np
import pytest

from shared.interfaces.stream_interface import (
    StreamBedUDPSender,
    StreamBedUDPReceiver,
    StreamFrame,
)
from shared.storage.frame_store import FrameStore

pytestmark = [pytest.mark.integration, pytest.mark.integration_stream]


def make_stream_frame(timestamp, frame_id):
    frame = np.random.randint(0, 256, (16, 16, 3), dtype=np.uint8)
    embedding = np.random.rand(64).astype(np.float32)
    return StreamFrame(
        timestamp=timestamp,
        frame=frame,
        embedding=embedding,
        model_version="v1",
        source_device_id=frame_id,
        frame_interleaving_rate=30.0,
    )


async def _collect_frames(n, sender_fn):
    """Collect `n` *logical* StreamFrames.

    Each logical frame the sender hands to `StreamBedUDPSender.send` is split
    on the wire into one CHNK (frame-only) and one EMBD (embedding-only)
    message. This helper waits for both halves of each logical frame and
    merges them by (source_device_id, timestamp), so callers can keep
    asserting on whole frames as if the split didn't happen.
    """
    receiver = StreamBedUDPReceiver()
    await receiver.listen("127.0.0.1", 0)
    port = receiver.get_local_port()

    sender = StreamBedUDPSender()
    await sender.connect("127.0.0.1", port)
    await sender_fn(sender)

    def _both_halves_present(sf: StreamFrame) -> bool:
        return sf.frame is not None and sf.embedding is not None

    by_key: dict = {}
    deadline = asyncio.get_running_loop().time() + 5.0
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        if len(by_key) >= n and all(_both_halves_present(sf) for sf in by_key.values()):
            break
        wire = await receiver.recv_one(timeout=min(2.0, remaining))
        if wire is None:
            break
        key = (wire.source_device_id, wire.timestamp)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = wire
        else:
            by_key[key] = StreamFrame(
                timestamp=existing.timestamp,
                frame=existing.frame if existing.frame is not None else wire.frame,
                embedding=existing.embedding if existing.embedding is not None else wire.embedding,
                model_version=existing.model_version,
                source_device_id=existing.source_device_id,
                frame_interleaving_rate=existing.frame_interleaving_rate,
            )

    await sender.close()
    await receiver.stop()
    return list(by_key.values())


def test_single_frame_lands_in_store(tmp_path):
    store = FrameStore(str(tmp_path))

    async def inner():
        sf = make_stream_frame(1000.0, "edge-001")
        async def send(sender):
            await sender.send(sf)
        return await _collect_frames(1, send)

    received = asyncio.run(inner())

    for r in received:
        store.store(r.source_device_id, r.timestamp, r.frame, r.embedding, r.model_version, 3600)

    assert store.count() == 1
    results = store.query_by_timestamp(0.0, 9999.0)
    assert results[0].frame_id == "edge-001"
    assert results[0].timestamp == 1000.0
    assert results[0].model_version == "v1"


def test_multiple_frames_all_stored(tmp_path):
    store = FrameStore(str(tmp_path))
    n = 5

    async def inner():
        async def send(sender):
            for i in range(n):
                await sender.send(make_stream_frame(float(i * 1000), f"edge-{i:03d}"))
                await asyncio.sleep(0.01)
        return await _collect_frames(n, send)

    received = asyncio.run(inner())

    for r in received:
        store.store(r.source_device_id, r.timestamp, r.frame, r.embedding, r.model_version, 3600)

    assert store.count() == n


def test_frame_data_preserved_end_to_end(tmp_path):
    original = make_stream_frame(42.0, "edge-check")

    async def inner():
        async def send(sender):
            await sender.send(original)
        return await _collect_frames(1, send)

    received = asyncio.run(inner())

    assert len(received) == 1
    got = received[0]
    assert got.timestamp == original.timestamp
    assert got.model_version == original.model_version
    assert got.frame_interleaving_rate == original.frame_interleaving_rate
    np.testing.assert_array_equal(got.frame, original.frame)
    np.testing.assert_array_almost_equal(got.embedding, original.embedding)


def test_embedding_retrievable_after_storage(tmp_path):
    store = FrameStore(str(tmp_path))
    original = make_stream_frame(100.0, "edge-emb")

    async def inner():
        async def send(sender):
            await sender.send(original)
        return await _collect_frames(1, send)

    received = asyncio.run(inner())

    for r in received:
        store.store(r.source_device_id, r.timestamp, r.frame, r.embedding, r.model_version, 3600)

    results = store.query_by_timestamp(0.0, 9999.0)
    assert results[0].embedding_path is not None
    stored_emb = np.load(results[0].embedding_path)
    np.testing.assert_array_almost_equal(stored_emb, original.embedding)

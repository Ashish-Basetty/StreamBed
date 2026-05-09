"""End-to-end pipeline test:

  edge inference (MobileNetV2) → split sender (CHNK + EMBD)
    → in-process receiver → server's store_from_stream_frame → FrameStore

Verifies that for every input frame:
  1. The edge-side model produces an embedding (inference happens).
  2. Both halves of the StreamFrame make it across the wire.
  3. The server's store ends up with one row per logical frame, carrying
     **both** a video file and an embedding file.
  4. The stored embedding bytes equal what the model emitted byte-for-byte.

The server in passthrough mode does no inference itself; the embedding is
generated edge-side and the server only persists it. This test pins that
contract.
"""
import asyncio
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

# Make `server.app.store_from_stream_frame` importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from shared.inference.mobilenet import MobileNetV2Model
from shared.interfaces.stream_interface import (
    StreamBedUDPReceiver,
    StreamBedUDPSender,
    StreamFrame,
)
from shared.storage.frame_store import FrameStore

pytestmark = [pytest.mark.integration, pytest.mark.integration_inference]

N_FRAMES = 6
SOURCE_ID = "edge-test"


def _frame_id(src: str, ts: float) -> str:
    # Mirror server.app._frame_id; duplicated to avoid importing server.app
    # (which has FastAPI + receiver side effects at import time).
    return f"{src}_{ts:.6f}"


def _store_from_stream_frame(target_store: FrameStore, sf: StreamFrame, ttl_seconds: float) -> None:
    target_store.store_partial(
        frame_id=_frame_id(sf.source_device_id, sf.timestamp),
        timestamp=sf.timestamp,
        frame=sf.frame,
        embedding=sf.embedding,
        model_version=sf.model_version or None,
        ttl_seconds=ttl_seconds,
    )


def _edge_inference_run(n: int) -> list[tuple[StreamFrame, np.ndarray]]:
    """Build N (StreamFrame, expected_embedding) pairs by actually running the
    MobileNetV2 model on synthetic frames. Returns the canonical embedding
    array alongside the frame so the test can later byte-compare against
    what landed in storage.
    """
    model = MobileNetV2Model()
    model.load()
    out: list[tuple[StreamFrame, np.ndarray]] = []
    for i in range(n):
        # Synthetic but valid 224x224x3 frame; varies per i so embeddings
        # differ.
        frame = (np.random.default_rng(seed=i).integers(0, 256, (224, 224, 3))
                 .astype(np.uint8))
        result = model.process_frame(frame)
        sf = StreamFrame(
            timestamp=1000.0 + i * 0.033,
            frame=frame,
            embedding=result.embedding,
            model_version=model.get_model_version(),
            source_device_id=SOURCE_ID,
            frame_interleaving_rate=30.0,
        )
        out.append((sf, result.embedding))
    return out


async def _run_pipeline(store: FrameStore, frames: list[StreamFrame]) -> None:
    """Send each frame via the split sender, drain the receiver, and call
    the server's store_from_stream_frame on each half. Returns when every
    logical frame has both halves in the store, or after a timeout.
    """
    receiver = StreamBedUDPReceiver()
    await receiver.listen("127.0.0.1", 0)
    port = receiver.get_local_port()
    assert port is not None

    sender = StreamBedUDPSender()
    await sender.connect("127.0.0.1", port)
    for sf in frames:
        await sender.send(sf)

    expected_logical = len(frames)
    deadline = asyncio.get_running_loop().time() + 15.0
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        # Stop once every expected (src, ts) row has both halves.
        rows = store.query_by_timestamp(0.0, 1e12)
        if (
            len(rows) >= expected_logical
            and all(r.frame_path and r.embedding_path for r in rows)
        ):
            break
        wire = await receiver.recv_one(timeout=min(2.0, remaining))
        if wire is None:
            break
        _store_from_stream_frame(store, wire, ttl_seconds=3600)

    await sender.close()
    await receiver.stop()


def test_edge_inference_streams_and_stores_every_embedding(tmp_path):
    edge = _edge_inference_run(N_FRAMES)
    frames = [sf for sf, _ in edge]
    # Edge ledger: what the edge actually sent, keyed by the same frame_id
    # the server uses, so we can do a 1:1 join against the server table.
    edge_ledger = {
        _frame_id(sf.source_device_id, sf.timestamp): {
            "timestamp": sf.timestamp,
            "frame_shape": sf.frame.shape,
            "embedding": emb,
            "model_version": sf.model_version,
        }
        for sf, emb in edge
    }

    store = FrameStore(str(tmp_path))
    asyncio.run(_run_pipeline(store, frames))

    rows = store.query_by_timestamp(0.0, 1e12)
    server_table = {r.frame_id: r for r in rows}

    # 1. Set equality: every edge frame_id appears server-side, no extras.
    assert set(server_table) == set(edge_ledger), (
        f"frame_id mismatch.\n"
        f"  only-edge:   {set(edge_ledger) - set(server_table)}\n"
        f"  only-server: {set(server_table) - set(edge_ledger)}"
    )

    # 2. Per-row join: every field the server stored matches the edge ledger.
    for fid, expected in edge_ledger.items():
        r = server_table[fid]

        # Both halves landed on disk.
        assert r.frame_path and Path(r.frame_path).exists(), f"{fid}: frame missing"
        assert r.embedding_path and Path(r.embedding_path).exists(), f"{fid}: embedding missing"

        # Timestamp survived the wire.
        assert r.timestamp == pytest.approx(expected["timestamp"]), (
            f"{fid}: timestamp drift {r.timestamp} vs {expected['timestamp']}"
        )

        # Model version survived the wire.
        assert r.model_version == expected["model_version"], (
            f"{fid}: model_version {r.model_version!r} vs {expected['model_version']!r}"
        )

        # Embedding bytes survived exactly (np.save is lossless).
        np.testing.assert_array_equal(np.load(r.embedding_path), expected["embedding"])

        # Frame survived structurally — JPEG is lossy so we can't byte-compare,
        # but shape must match what the edge sent.
        decoded = cv2.imread(r.frame_path)
        assert decoded is not None, f"{fid}: frame jpg unreadable"
        assert decoded.shape == expected["frame_shape"], (
            f"{fid}: frame shape {decoded.shape} vs edge {expected['frame_shape']}"
        )

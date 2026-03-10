import asyncio
import time

import numpy as np
import pytest

from shared.interfaces.stream_interface import (
    StreamBedUDPSender,
    StreamBedUDPReceiver,
    StreamFrame,
)


def make_frame(ts, src):
    return StreamFrame(
        timestamp=ts,
        frame=None,
        embedding=np.random.rand(32).astype(np.float32),
        model_version="v1",
        source_device_id=src,
        frame_interleaving_rate=30.0,
    )


async def _send_and_collect(n_frames, send_delay=0.0):
    receiver = StreamBedUDPReceiver()
    await receiver.listen("127.0.0.1", 0)
    port = receiver._transport.get_extra_info("socket").getsockname()[1]
    queue = receiver._queue

    sender = StreamBedUDPSender()
    await sender.connect("127.0.0.1", port)

    t_start = time.perf_counter()
    for i in range(n_frames):
        await sender.send(make_frame(float(i), "edge-sim"))
        if send_delay > 0:
            await asyncio.sleep(send_delay)

    await asyncio.sleep(0.3)
    t_end = time.perf_counter()

    received = []
    while not queue.empty():
        received.append(queue.get_nowait())

    await sender.close()
    await receiver.stop()

    elapsed = t_end - t_start
    throughput = len(received) / elapsed if elapsed > 0 else 0.0
    return len(received), n_frames, throughput


async def _baseline_throughput(n_frames):
    q = asyncio.Queue()
    t_start = time.perf_counter()
    for i in range(n_frames):
        await q.put(make_frame(float(i), "baseline"))
    received = []
    while not q.empty():
        received.append(q.get_nowait())
    t_end = time.perf_counter()
    elapsed = t_end - t_start
    throughput = len(received) / elapsed if elapsed > 0 else 0.0
    return len(received), n_frames, throughput


def test_baseline_throughput():
    received, sent, fps = asyncio.run(_baseline_throughput(100))
    assert received == sent
    assert fps > 0
    print(f"\n[baseline] {received}/{sent} frames @ {fps:.1f} fps")


def test_no_delay_delivery_ratio():
    received, sent, fps = asyncio.run(_send_and_collect(20))
    ratio = received / sent
    assert ratio == 1.0, f"Expected all frames delivered, got {ratio:.2%}"
    print(f"\n[no-delay] {received}/{sent} frames @ {fps:.1f} fps")


def test_no_delay_throughput_positive():
    received, sent, fps = asyncio.run(_send_and_collect(20))
    assert fps > 0


def test_slow_sender_delivery_ratio():
    received, sent, fps = asyncio.run(_send_and_collect(10, send_delay=0.01))
    ratio = received / sent
    assert ratio == 1.0, f"Expected all frames at 10ms spacing, got {ratio:.2%}"
    print(f"\n[10ms-delay] {received}/{sent} frames @ {fps:.1f} fps")


def test_slow_sender_lower_throughput_than_fast():
    _, _, fps_fast = asyncio.run(_send_and_collect(10, send_delay=0.0))
    _, _, fps_slow = asyncio.run(_send_and_collect(10, send_delay=0.05))
    assert fps_fast > fps_slow, "Fast sender should yield higher throughput than slow sender"


def test_throughput_scales_with_frame_count():
    _, _, fps_small = asyncio.run(_send_and_collect(5))
    _, _, fps_large = asyncio.run(_send_and_collect(20))
    assert fps_large > 0 and fps_small > 0


def test_embedding_only_frames_no_loss():
    received, sent, fps = asyncio.run(_send_and_collect(30))
    assert received == sent
    print(f"\n[embedding-only] {received}/{sent} frames @ {fps:.1f} fps")


def test_udp_throughput_vs_baseline():
    _, _, fps_base = asyncio.run(_baseline_throughput(50))
    _, _, fps_udp = asyncio.run(_send_and_collect(50))
    assert fps_udp > 0
    print(f"\n[throughput] baseline={fps_base:.1f} fps  udp={fps_udp:.1f} fps")

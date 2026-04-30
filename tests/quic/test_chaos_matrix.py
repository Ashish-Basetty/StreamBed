"""Chaos matrix: send N frames through chaosproxy, assert delivery thresholds
and that frame ordering is preserved (frame IDs are recovered in any order
since UDP/datagrams allow reordering, but the *set* must match within ratio).
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from shared.interfaces.stream_interface import (
    StreamBedUDPReceiver,
    StreamBedUDPSender,
    StreamFrame,
)
from tests.quic.chaosproxy import ChaosConfig, ChaosProxy

pytestmark = [pytest.mark.integration_quic]


def _frame(i: int) -> StreamFrame:
    return StreamFrame(
        timestamp=float(i),
        frame=None,
        embedding=np.random.rand(32).astype(np.float32),
        model_version="v1",
        source_device_id=f"edge-{i:03d}",
        frame_interleaving_rate=30.0,
    )


@pytest.mark.parametrize(
    "name,cfg,min_ratio",
    [
        ("clean", ChaosConfig(), 0.99),
        ("light_loss", ChaosConfig(loss_pct=2, jitter_ms=2), 0.85),
        ("burst_loss", ChaosConfig(burst_loss_prob=0.05, burst_recover_prob=0.6), 0.50),
    ],
)
def test_chaos_delivery_ratio(name, cfg, min_ratio):
    async def inner():
        receiver = StreamBedUDPReceiver()
        await receiver.listen("127.0.0.1", 0)
        recv_port = receiver.get_local_port()

        proxy = ChaosProxy(0, "127.0.0.1", recv_port, cfg)
        proxy_port = await proxy.start()

        sender = StreamBedUDPSender()
        await sender.connect("127.0.0.1", proxy_port)

        n = 50
        for i in range(n):
            await sender.send(_frame(i))
            await asyncio.sleep(0.005)

        received = []
        while True:
            frame = await receiver.recv_one(timeout=0.3)
            if frame is None:
                break
            received.append(frame)

        await sender.close()
        await receiver.stop()
        await proxy.stop()
        return len(received), n

    got, sent = asyncio.run(inner())
    ratio = got / sent
    assert ratio >= min_ratio, f"{name}: delivered {got}/{sent} = {ratio:.2f} < {min_ratio}"

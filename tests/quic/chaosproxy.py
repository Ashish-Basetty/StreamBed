"""UDP chaos proxy for QUIC reliability tests.

Generalizes tests/throughput/proxy.py with: jitter, loss bursts (Gilbert-Elliott),
duplication, and reordering. Configured via env vars or kwargs.

Usage as a script:
    LISTEN_PORT=9001 FORWARD_HOST=server FORWARD_PORT=9000 \
    LOSS_PCT=5 JITTER_MS=10 BURST_LOSS_PROB=0.05 BURST_LEN=8 \
    python -m tests.quic.chaosproxy

Importable for in-process tests: see ChaosProxy.start().
"""
from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass


@dataclass
class ChaosConfig:
    """Knobs for chaos injection. All fractions are 0.0-1.0."""

    loss_pct: float = 0.0           # baseline iid loss probability (0..100)
    delay_ms: float = 0.0           # constant delay
    jitter_ms: float = 0.0          # gaussian jitter sigma
    dup_pct: float = 0.0            # duplication probability (0..100)
    reorder_window: int = 0         # if >0, holds packets in a buffer of this size and emits in random order
    burst_loss_prob: float = 0.0    # P(enter "bad" state) per packet
    burst_recover_prob: float = 0.5 # P(leave "bad" state) per packet while in it

    @classmethod
    def from_env(cls) -> "ChaosConfig":
        f = lambda k, d: float(os.getenv(k, d))
        i = lambda k, d: int(os.getenv(k, d))
        return cls(
            loss_pct=f("LOSS_PCT", 0),
            delay_ms=f("DELAY_MS", 0),
            jitter_ms=f("JITTER_MS", 0),
            dup_pct=f("DUP_PCT", 0),
            reorder_window=i("REORDER_WINDOW", 0),
            burst_loss_prob=f("BURST_LOSS_PROB", 0),
            burst_recover_prob=f("BURST_RECOVER_PROB", 0.5),
        )


class _ProxyProto(asyncio.DatagramProtocol):
    def __init__(self, fwd_transport: asyncio.DatagramTransport, cfg: ChaosConfig):
        self._fwd = fwd_transport
        self._cfg = cfg
        self._in_burst = False
        self._reorder_buf: list[bytes] = []

    def datagram_received(self, data: bytes, addr):
        # Gilbert-Elliott: bursty correlated loss.
        if self._in_burst:
            if random.random() < self._cfg.burst_recover_prob:
                self._in_burst = False
            else:
                return
        elif random.random() < self._cfg.burst_loss_prob:
            self._in_burst = True
            return

        if random.random() * 100 < self._cfg.loss_pct:
            return

        copies = 1 + (1 if random.random() * 100 < self._cfg.dup_pct else 0)
        for _ in range(copies):
            asyncio.create_task(self._deliver(data))

    async def _deliver(self, data: bytes):
        delay = self._cfg.delay_ms
        if self._cfg.jitter_ms > 0:
            delay = max(0.0, delay + random.gauss(0, self._cfg.jitter_ms))
        if delay > 0:
            await asyncio.sleep(delay / 1000)

        if self._cfg.reorder_window > 0:
            self._reorder_buf.append(data)
            if len(self._reorder_buf) >= self._cfg.reorder_window:
                random.shuffle(self._reorder_buf)
                for p in self._reorder_buf:
                    self._fwd.sendto(p)
                self._reorder_buf.clear()
            return

        self._fwd.sendto(data)

    def error_received(self, exc):
        pass


class ChaosProxy:
    """Programmatic in-process chaos proxy for tests."""

    def __init__(self, listen_port: int, forward_host: str, forward_port: int, cfg: ChaosConfig):
        self.listen_port = listen_port
        self.forward = (forward_host, forward_port)
        self.cfg = cfg
        self._listen_transport: asyncio.DatagramTransport | None = None
        self._fwd_transport: asyncio.DatagramTransport | None = None

    async def start(self) -> int:
        loop = asyncio.get_running_loop()
        self._fwd_transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=self.forward
        )
        self._listen_transport, _ = await loop.create_datagram_endpoint(
            lambda: _ProxyProto(self._fwd_transport, self.cfg),
            local_addr=("127.0.0.1", self.listen_port),
        )
        bound = self._listen_transport.get_extra_info("socket").getsockname()[1]
        return bound

    async def stop(self) -> None:
        if self._listen_transport:
            self._listen_transport.close()
        if self._fwd_transport:
            self._fwd_transport.close()


async def _main():
    cfg = ChaosConfig.from_env()
    proxy = ChaosProxy(
        listen_port=int(os.getenv("PROXY_LISTEN_PORT", "9001")),
        forward_host=os.getenv("FORWARD_HOST", "server"),
        forward_port=int(os.getenv("FORWARD_PORT", "9000")),
        cfg=cfg,
    )
    bound = await proxy.start()
    print(f"[chaosproxy] listening on 127.0.0.1:{bound} -> {proxy.forward} cfg={cfg}")
    await asyncio.sleep(float("inf"))


if __name__ == "__main__":
    asyncio.run(_main())

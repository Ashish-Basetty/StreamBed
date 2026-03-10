import asyncio
import os
import random

LISTEN_PORT = int(os.getenv("PROXY_LISTEN_PORT", "9001"))
FORWARD_HOST = os.getenv("FORWARD_HOST", "server")
FORWARD_PORT = int(os.getenv("FORWARD_PORT", "9000"))
DELAY_MS = float(os.getenv("DELAY_MS", "0"))
LOSS_PCT = float(os.getenv("LOSS_PCT", "0"))


class ProxyProtocol(asyncio.DatagramProtocol):
    def __init__(self, forward_transport):
        self._fwd = forward_transport

    def datagram_received(self, data, addr):
        if random.random() * 100 < LOSS_PCT:
            return
        asyncio.create_task(self._forward(data))

    async def _forward(self, data):
        if DELAY_MS > 0:
            await asyncio.sleep(DELAY_MS / 1000)
        self._fwd.sendto(data)

    def error_received(self, exc):
        pass

    def connection_lost(self, exc):
        pass


async def main():
    loop = asyncio.get_running_loop()
    fwd_transport, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol,
        remote_addr=(FORWARD_HOST, FORWARD_PORT),
    )
    await loop.create_datagram_endpoint(
        lambda: ProxyProtocol(fwd_transport),
        local_addr=("0.0.0.0", LISTEN_PORT),
    )
    print(f"[proxy] listening on 0.0.0.0:{LISTEN_PORT} -> {FORWARD_HOST}:{FORWARD_PORT} delay={DELAY_MS}ms loss={LOSS_PCT}%")
    await asyncio.sleep(float("inf"))


asyncio.run(main())

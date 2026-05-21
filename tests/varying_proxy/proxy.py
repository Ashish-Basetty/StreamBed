"""
Time-varying UDP forwarder for the StreamBed experiment harness.

Forwards UDP datagrams between the edge sidecar (inbound) and the server
sidecar (upstream). The QUIC session between edge and server is end-to-end —
this proxy never terminates or parses QUIC, it just shapes the underlying
UDP packet stream:

  edge ──┐                                ┌──► server
         └──► [token bucket + loss + lat] ┘
              (edge→server direction only)
              return path forwards unthrottled

Reads two mounted files polled every POLL_INTERVAL seconds:

  SCHEDULE_PATH  YAML list of  {at_s, bps, loss_pct, extra_latency_ms}
  TARGET_PATH    YAML          {target_host, target_port}

Environment:
  BIND_HOST            default 0.0.0.0
  BIND_PORT            default 9010
  SCHEDULE_PATH        default /etc/streambed/schedule.yml
  TARGET_PATH          default /etc/streambed/target.yml
  POLL_INTERVAL        default 0.5 (seconds)
  T0_OFFSET_S          start the schedule clock this many seconds in the past
  IDLE_TIMEOUT_S       drop a flow after this much inactivity (default 120)
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from pathlib import Path

import yaml


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


BIND_HOST = _env("BIND_HOST", "0.0.0.0")
BIND_PORT = int(_env("BIND_PORT", "9010"))
SCHEDULE_PATH = Path(_env("SCHEDULE_PATH", "/etc/streambed/schedule.yml"))
TARGET_PATH = Path(_env("TARGET_PATH", "/etc/streambed/target.yml"))
POLL_INTERVAL = float(_env("POLL_INTERVAL", "0.5"))
T0_OFFSET_S = float(_env("T0_OFFSET_S", "0"))
IDLE_TIMEOUT_S = float(_env("IDLE_TIMEOUT_S", "120"))


class State:
    def __init__(self) -> None:
        self.bps: float = 1e12
        self.loss_pct: float = 0.0
        self.extra_latency_ms: float = 0.0
        self.target_host: str | None = None
        self.target_port: int | None = None
        # Token-bucket state (shared across flows — models a single bottleneck link)
        self.tokens: float = 0.0
        self.last_fill: float = time.monotonic()
        # Bumped whenever target changes; flows started under an old generation
        # are torn down by the idle reaper so they reconnect to the new target.
        self.target_generation: int = 0


def _load_schedule(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        data = yaml.safe_load(f) or []
    return [dict(seg) for seg in data]


def _load_target(path: Path) -> tuple[str | None, int | None]:
    if not path.exists():
        return None, None
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    host = data.get("target_host")
    port = data.get("target_port")
    if host and isinstance(port, int) and port > 0:
        return host, port
    return None, None


async def _schedule_runner(state: State, started_at: float) -> None:
    last_seen: list[dict] | None = None
    while True:
        try:
            segs = _load_schedule(SCHEDULE_PATH)
        except Exception as e:  # noqa: BLE001
            print(f"[proxy] schedule read error: {e}", flush=True)
            segs = last_seen or []
        if segs != last_seen:
            print(f"[proxy] schedule loaded: {segs}", flush=True)
            last_seen = segs
        if segs:
            elapsed = time.monotonic() - started_at + T0_OFFSET_S
            active = None
            for seg in segs:
                if elapsed >= float(seg.get("at_s", 0)):
                    active = seg
                else:
                    break
            if active is not None:
                bps = float(active.get("bps", state.bps))
                loss = float(active.get("loss_pct", 0.0))
                lat = float(active.get("extra_latency_ms", 0.0))
                if (bps, loss, lat) != (state.bps, state.loss_pct, state.extra_latency_ms):
                    print(f"[proxy] t={elapsed:.1f}s → bps={bps} loss={loss} lat_ms={lat}", flush=True)
                state.bps = bps
                state.loss_pct = loss
                state.extra_latency_ms = lat
        await asyncio.sleep(POLL_INTERVAL)


async def _target_runner(state: State) -> None:
    last: tuple[str | None, int | None] = (None, None)
    while True:
        try:
            host, port = _load_target(TARGET_PATH)
        except Exception as e:  # noqa: BLE001
            print(f"[proxy] target read error: {e}", flush=True)
            host, port = last
        if (host, port) != last:
            print(f"[proxy] target → {host}:{port}", flush=True)
            last = (host, port)
            state.target_generation += 1
        state.target_host = host
        state.target_port = port
        await asyncio.sleep(POLL_INTERVAL)


def _should_drop(state: State, n: int) -> bool:
    """Token-bucket decision for edge→server UDP datagrams.

    Returns True when the bucket is empty and the packet should be dropped.
    The bucket holds at most one second of bandwidth (or one packet, whichever
    is larger) so brief bursts pass through but sustained over-rate traffic
    is shaped.
    """
    bps = max(state.bps, 1.0)
    bytes_per_sec = bps / 8.0
    now = time.monotonic()
    burst = max(bytes_per_sec, float(n))
    state.tokens = min(burst, state.tokens + (now - state.last_fill) * bytes_per_sec)
    state.last_fill = now
    if state.tokens < n:
        return True
    state.tokens -= n
    return False


class _UpstreamProtocol(asyncio.DatagramProtocol):
    """Per-flow upstream socket. Forwards server→edge return packets back to
    the original edge client address through the inbound transport."""

    def __init__(self, flow: Flow) -> None:
        self._flow = flow

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        # Return path is unthrottled — FBCK, ACKs, and stream data flow freely.
        self._flow.last_activity = time.monotonic()
        inbound = self._flow.inbound_transport
        if inbound is not None:
            inbound.sendto(data, self._flow.client_addr)

    def error_received(self, exc: Exception) -> None:
        print(f"[proxy] upstream socket error on {self._flow.client_addr}: {exc}", flush=True)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            print(f"[proxy] upstream socket closed for {self._flow.client_addr}: {exc}", flush=True)


class Flow:
    __slots__ = ("client_addr", "inbound_transport", "upstream_transport",
                 "last_activity", "generation")

    def __init__(self, client_addr, inbound_transport,
                 upstream_transport, generation: int) -> None:  # noqa: ANN001
        self.client_addr = client_addr
        self.inbound_transport = inbound_transport
        self.upstream_transport = upstream_transport
        self.last_activity = time.monotonic()
        self.generation = generation


class InboundProtocol(asyncio.DatagramProtocol):
    """UDP socket bound to BIND_HOST:BIND_PORT. Demultiplexes edge clients by
    source (ip, port) and creates a per-client upstream socket on demand."""

    def __init__(self, state: State, loop: asyncio.AbstractEventLoop) -> None:
        self._state = state
        self._loop = loop
        self._transport: asyncio.DatagramTransport | None = None
        self._flows: dict[tuple, Flow] = {}
        self._pending: set[tuple] = set()  # client_addrs with upstream in-flight

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        print(f"[proxy] UDP listening {BIND_HOST}:{BIND_PORT}", flush=True)

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        state = self._state
        # Token bucket + loss model applied to edge→server direction only.
        if _should_drop(state, len(data)):
            return
        if state.loss_pct > 0.0 and random.random() < state.loss_pct:
            return

        flow = self._flows.get(addr)
        if flow is None:
            if addr in self._pending:
                # Upstream socket is still being created; drop this packet.
                # QUIC will retransmit. Avoids buffering early bytes.
                return
            host = state.target_host
            port = state.target_port
            if host is None or port is None:
                return
            self._pending.add(addr)
            self._loop.create_task(self._open_flow(addr, host, port, state.target_generation))
            return

        if state.extra_latency_ms > 0.0:
            self._loop.call_later(
                state.extra_latency_ms / 1000.0,
                _send_upstream, flow, data,
            )
        else:
            _send_upstream(flow, data)

    async def _open_flow(self, client_addr, host: str, port: int, generation: int) -> None:  # noqa: ANN001
        try:
            flow_ref: list[Flow] = []

            def _factory() -> _UpstreamProtocol:
                return _UpstreamProtocol(flow_ref[0])

            flow = Flow(
                client_addr=client_addr,
                inbound_transport=self._transport,
                upstream_transport=None,  # type: ignore[arg-type]
                generation=generation,
            )
            flow_ref.append(flow)
            transport, _ = await self._loop.create_datagram_endpoint(
                _factory,
                remote_addr=(host, port),
            )
            flow.upstream_transport = transport  # type: ignore[assignment]
            self._flows[client_addr] = flow
            print(f"[proxy] flow {client_addr} → {host}:{port}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[proxy] upstream open failed for {client_addr}: {e}", flush=True)
        finally:
            self._pending.discard(client_addr)

    def error_received(self, exc: Exception) -> None:
        print(f"[proxy] inbound socket error: {exc}", flush=True)

    async def reap(self) -> None:
        """Periodically close idle flows and flows from stale target generations."""
        while True:
            await asyncio.sleep(IDLE_TIMEOUT_S / 2)
            now = time.monotonic()
            gen = self._state.target_generation
            dead = [
                addr for addr, f in self._flows.items()
                if now - f.last_activity > IDLE_TIMEOUT_S or f.generation != gen
            ]
            for addr in dead:
                flow = self._flows.pop(addr, None)
                if flow and flow.upstream_transport is not None:
                    flow.upstream_transport.close()
                print(f"[proxy] flow {addr} closed", flush=True)


def _send_upstream(flow: Flow, data: bytes) -> None:
    if flow.upstream_transport is None:
        return
    flow.last_activity = time.monotonic()
    flow.upstream_transport.sendto(data)


async def run_proxy() -> None:
    state = State()
    started_at = time.monotonic()
    loop = asyncio.get_running_loop()

    asyncio.create_task(_schedule_runner(state, started_at))
    asyncio.create_task(_target_runner(state))

    inbound = InboundProtocol(state, loop)
    await loop.create_datagram_endpoint(
        lambda: inbound,
        local_addr=(BIND_HOST, BIND_PORT),
    )
    asyncio.create_task(inbound.reap())

    print(f"[proxy] schedule={SCHEDULE_PATH} target={TARGET_PATH}", flush=True)
    await asyncio.Future()


def main() -> None:
    asyncio.run(run_proxy())


if __name__ == "__main__":
    main()

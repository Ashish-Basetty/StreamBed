"""
Time-varying UDP proxy for the StreamBed experiment harness.

Sits between the edge sidecar and the server sidecar's published host UDP port,
applying a stepped schedule of throughput / loss / latency. Reads two mounted
files, both polled every POLL_INTERVAL seconds so the test can update them
without rebuilding or restarting the container:

  SCHEDULE_PATH  YAML list of  {at_s, bps, loss_pct, extra_latency_ms}
  TARGET_PATH    YAML/plain    target_host:target_port

Environment:
  BIND_HOST            default 0.0.0.0
  BIND_PORT            default 9010
  SCHEDULE_PATH        default /etc/streambed/schedule.yml
  TARGET_PATH          default /etc/streambed/target.yml
  POLL_INTERVAL        default 0.5 (seconds)
  T0_OFFSET_S          start the schedule clock this many seconds in the past
                        (lets the test pre-arm before pumping traffic)
"""
import asyncio
import os
import random
import socket
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


class State:
    """Mutable knobs that the schedule task rewrites and the recv loop reads.

    All fields are plain Python attrs guarded by GIL semantics — single-writer,
    multi-reader is safe without an explicit lock for atomic scalar assignment.
    """

    def __init__(self) -> None:
        self.bps: float = 1e12  # effectively unthrottled until schedule kicks in
        self.loss_pct: float = 0.0
        self.extra_latency_ms: float = 0.0
        self.target_host: str | None = None
        self.target_port: int | None = None


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
    if host and isinstance(port, int):
        return host, port
    return None, None


async def _schedule_runner(state: State, started_at: float) -> None:
    """Apply the proxy schedule by wall-clock. Re-reads schedule.yml each tick
    so the test can hot-update the script."""
    last_seen_segs: list[dict] | None = None
    while True:
        try:
            segs = _load_schedule(SCHEDULE_PATH)
        except Exception as e:  # noqa: BLE001
            print(f"[VaryingProxy] schedule read error: {e}", flush=True)
            segs = last_seen_segs or []
        if segs != last_seen_segs:
            print(f"[VaryingProxy] schedule loaded: {segs}", flush=True)
            last_seen_segs = segs

        if segs:
            elapsed = time.monotonic() - started_at + T0_OFFSET_S
            active = None
            for seg in segs:
                at = float(seg.get("at_s", 0))
                if elapsed >= at:
                    active = seg
                else:
                    break
            if active is not None:
                bps = float(active.get("bps", state.bps))
                loss = float(active.get("loss_pct", 0.0))
                lat = float(active.get("extra_latency_ms", 0.0))
                if (bps, loss, lat) != (state.bps, state.loss_pct, state.extra_latency_ms):
                    print(
                        f"[VaryingProxy] t={elapsed:.1f}s → bps={bps} loss={loss} lat_ms={lat}",
                        flush=True,
                    )
                state.bps = bps
                state.loss_pct = loss
                state.extra_latency_ms = lat
        await asyncio.sleep(POLL_INTERVAL)


async def _target_runner(state: State) -> None:
    last_target: tuple[str | None, int | None] = (None, None)
    while True:
        try:
            host, port = _load_target(TARGET_PATH)
        except Exception as e:  # noqa: BLE001
            print(f"[VaryingProxy] target read error: {e}", flush=True)
            host, port = last_target
        if (host, port) != last_target:
            print(f"[VaryingProxy] forwarding target → {host}:{port}", flush=True)
            last_target = (host, port)
        state.target_host = host
        state.target_port = port
        await asyncio.sleep(POLL_INTERVAL)


async def run_proxy() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((BIND_HOST, BIND_PORT))
    sock.setblocking(False)

    loop = asyncio.get_running_loop()
    state = State()
    started_at = time.monotonic()

    asyncio.create_task(_schedule_runner(state, started_at))
    asyncio.create_task(_target_runner(state))

    tokens = 0.0
    last_fill = time.monotonic()

    print(
        f"[VaryingProxy] listening {BIND_HOST}:{BIND_PORT}, "
        f"schedule={SCHEDULE_PATH}, target={TARGET_PATH}",
        flush=True,
    )

    while True:
        try:
            data, _src = await loop.sock_recvfrom(sock, 65536)
        except Exception as e:  # noqa: BLE001
            print(f"[VaryingProxy] recv error: {e}", flush=True)
            break

        if state.target_host is None or state.target_port is None:
            # No target yet; drop until the test mounts target.yml.
            continue

        # Token-bucket refill at current bps.
        now = time.monotonic()
        bps = max(state.bps, 1.0)
        bytes_per_sec = bps / 8.0
        burst = max(bytes_per_sec, float(len(data)))  # always admit a full datagram
        tokens = min(burst, tokens + (now - last_fill) * bytes_per_sec)
        last_fill = now

        need = len(data)
        if need > tokens:
            await asyncio.sleep((need - tokens) / bytes_per_sec)
            tokens = 0.0
            last_fill = time.monotonic()
        else:
            tokens -= need

        if state.loss_pct > 0.0 and random.random() < state.loss_pct:
            continue

        if state.extra_latency_ms > 0.0:
            await asyncio.sleep(state.extra_latency_ms / 1000.0)

        try:
            await loop.sock_sendto(
                sock, data, (state.target_host, state.target_port)
            )
        except Exception as e:  # noqa: BLE001
            print(f"[VaryingProxy] send error: {e}", flush=True)


def main() -> None:
    asyncio.run(run_proxy())


if __name__ == "__main__":
    main()

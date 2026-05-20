"""
Time-varying QUIC relay for the StreamBed experiment harness.

Terminates the edge sidecar's QUIC connection, then re-dials to the server
sidecar. Applies a token-bucket rate limit + random loss on datagrams flowing
edge→server. Return traffic (server→edge) passes unthrottled so FBCK and QUIC
ACKs are never rate-limited.

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
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import random
import ssl
import tempfile
import time
from pathlib import Path

import yaml
from aioquic.asyncio import connect, serve
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import DatagramFrameReceived, QuicEvent, StreamDataReceived


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


BIND_HOST = _env("BIND_HOST", "0.0.0.0")
BIND_PORT = int(_env("BIND_PORT", "9010"))
SCHEDULE_PATH = Path(_env("SCHEDULE_PATH", "/etc/streambed/schedule.yml"))
TARGET_PATH = Path(_env("TARGET_PATH", "/etc/streambed/target.yml"))
POLL_INTERVAL = float(_env("POLL_INTERVAL", "0.5"))
T0_OFFSET_S = float(_env("T0_OFFSET_S", "0"))


class State:
    def __init__(self) -> None:
        self.bps: float = 1e12
        self.loss_pct: float = 0.0
        self.extra_latency_ms: float = 0.0
        self.target_host: str | None = None
        self.target_port: int | None = None
        # Token-bucket state (shared across connections — single-relay model)
        self.tokens: float = 0.0
        self.last_fill: float = time.monotonic()


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
        state.target_host = host
        state.target_port = port
        await asyncio.sleep(POLL_INTERVAL)


def _should_drop(state: State, n: int) -> bool:
    """Token-bucket drop decision for edge→server datagrams."""
    bps = max(state.bps, 1.0)
    bytes_per_sec = bps / 8.0
    now = time.monotonic()
    burst = max(bytes_per_sec, float(n))
    state.tokens = min(burst, state.tokens + (now - state.last_fill) * bytes_per_sec)
    state.last_fill = now
    if state.tokens < n:
        return True  # drop
    state.tokens -= n
    return False


class RelayProtocol(QuicConnectionProtocol):
    """Inbound QUIC connection from the edge sidecar.

    On first event, dials outbound QUIC to the server sidecar and relays
    datagrams/streams bidirectionally. Edge→server datagrams are rate-limited;
    server→edge traffic (FBCK, ACKs, stream data) is forwarded unthrottled.
    """

    def __init__(self, *args, state: State, server_cfg: QuicConfiguration, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._state = state
        self._server_cfg = server_cfg
        self._upstream: QuicConnectionProtocol | None = None
        self._connect_lock = asyncio.Lock()
        self._streams_edge: dict[int, int] = {}   # edge stream_id → upstream stream_id
        self._streams_up: dict[int, int] = {}     # upstream stream_id → edge stream_id

    async def _ensure_upstream(self) -> QuicConnectionProtocol | None:
        async with self._connect_lock:
            if self._upstream is not None:
                return self._upstream
            host = self._state.target_host
            port = self._state.target_port
            if host is None or port is None:
                return None
            connected_ev = asyncio.Event()
            relay_ref = self

            async def _hold_upstream() -> None:
                try:
                    def _make_upstream(*args, **kwargs):  # noqa: ANN001
                        return UpstreamProtocol(*args, relay=relay_ref, **kwargs)
                    async with connect(
                        host, port,
                        configuration=self._server_cfg,
                        create_protocol=_make_upstream,
                    ) as proto:
                        relay_ref._upstream = proto
                        print(f"[proxy] upstream QUIC connected to {host}:{port}", flush=True)
                        connected_ev.set()
                        await asyncio.Future()  # hold connection open
                except Exception as e:  # noqa: BLE001
                    print(f"[proxy] upstream lost: {e}", flush=True)
                    relay_ref._upstream = None

            asyncio.create_task(_hold_upstream())
            try:
                await asyncio.wait_for(connected_ev.wait(), timeout=10.0)
            except TimeoutError:
                print(f"[proxy] upstream connect timeout to {host}:{port}", flush=True)
                return None
        return self._upstream

    def quic_event_received(self, event: QuicEvent) -> None:  # type: ignore[override]
        if isinstance(event, DatagramFrameReceived):
            asyncio.ensure_future(self._relay_datagram_up(event.data))
        elif isinstance(event, StreamDataReceived):
            asyncio.ensure_future(self._relay_stream_up(event.stream_id, event.data, event.end_stream))

    async def _relay_datagram_up(self, data: bytes) -> None:
        state = self._state
        # Rate limit + random loss on edge→server datagrams.
        if _should_drop(state, len(data)):
            return
        if state.loss_pct > 0.0 and random.random() < state.loss_pct:
            return
        if state.extra_latency_ms > 0.0:
            await asyncio.sleep(state.extra_latency_ms / 1000.0)
        up = await self._ensure_upstream()
        if up is None:
            return
        up._quic.send_datagram_frame(data)
        up.transmit()

    async def _relay_stream_up(self, stream_id: int, data: bytes, end_stream: bool) -> None:
        up = await self._ensure_upstream()
        if up is None:
            return
        if stream_id not in self._streams_edge:
            us_id = up._quic.get_next_available_stream_id()
            self._streams_edge[stream_id] = us_id
            self._streams_up[us_id] = stream_id
            print(f"[proxy] stream map edge:{stream_id}→up:{us_id}", flush=True)
        us_id = self._streams_edge[stream_id]
        print(f"[proxy] stream↑ edge:{stream_id}→up:{us_id} {len(data)}B hdr={data[:4].hex()}", flush=True)
        up._quic.send_stream_data(us_id, data, end_stream)
        up.transmit()

    def relay_datagram_down(self, data: bytes) -> None:
        """Called by upstream protocol when server sends a datagram."""
        self._quic.send_datagram_frame(data)
        self.transmit()

    def relay_stream_down(self, upstream_stream_id: int, data: bytes, end_stream: bool) -> None:
        edge_id = self._streams_up.get(upstream_stream_id)
        if edge_id is None:
            print(f"[proxy] stream↓ UNMAPPED up:{upstream_stream_id} {len(data)}B hdr={data[:4].hex()} — dropping", flush=True)
            return
        print(f"[proxy] stream↓ up:{upstream_stream_id}→edge:{edge_id} {len(data)}B hdr={data[:4].hex()}", flush=True)
        self._quic.send_stream_data(edge_id, data, end_stream)
        self.transmit()


class UpstreamProtocol(QuicConnectionProtocol):
    """Outbound QUIC connection to the server sidecar."""

    def __init__(self, *args, relay: RelayProtocol, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._relay = relay

    def quic_event_received(self, event: QuicEvent) -> None:  # type: ignore[override]
        if isinstance(event, DatagramFrameReceived):
            self._relay.relay_datagram_down(event.data)
        elif isinstance(event, StreamDataReceived):
            self._relay.relay_stream_down(event.stream_id, event.data, event.end_stream)


ALPN = "streambed-quic-v1"


def _make_server_config() -> QuicConfiguration:
    cfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN])
    cfg.verify_mode = ssl.CERT_NONE  # edge DevTLSConfig uses InsecureSkipVerify
    cfg.max_datagram_frame_size = 65536
    return cfg


def _make_inbound_config() -> QuicConfiguration:
    """Self-signed cert — edge sidecar skips verification (InsecureSkipVerify)."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "streambed-varying-proxy")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("streambed-varying-proxy")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    # Write to temp files for aioquic
    cert_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    cert_file.write(cert_pem)
    cert_file.flush()
    key_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    key_file.write(key_pem)
    key_file.flush()

    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.load_cert_chain(cert_file.name, key_file.name)
    cfg.max_datagram_frame_size = 65536
    return cfg


async def run_proxy() -> None:
    state = State()
    started_at = time.monotonic()

    asyncio.create_task(_schedule_runner(state, started_at))
    asyncio.create_task(_target_runner(state))

    inbound_cfg = _make_inbound_config()
    server_cfg = _make_server_config()

    def protocol_factory(*args, **kwargs):  # noqa: ANN001
        return RelayProtocol(*args, state=state, server_cfg=server_cfg, **kwargs)

    print(f"[proxy] QUIC relay listening {BIND_HOST}:{BIND_PORT}", flush=True)
    print(f"[proxy] schedule={SCHEDULE_PATH} target={TARGET_PATH}", flush=True)

    await serve(
        BIND_HOST, BIND_PORT,
        configuration=inbound_cfg,
        create_protocol=protocol_factory,
    )
    await asyncio.Future()  # run forever


def main() -> None:
    asyncio.run(run_proxy())


if __name__ == "__main__":
    main()

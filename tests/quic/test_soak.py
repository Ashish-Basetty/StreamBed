"""Soak test: spawn a sidecar pair, drive sustained traffic, scrape /metrics
and per-process RSS at 5s cadence, assert no leak (RSS slope after warmup is
close to zero).

Default duration is short (30s) so the test is runnable on a developer box;
override with `STREAMBED_SOAK_DURATION_SECS` for the weekly long-run job.
Skipped unless `STREAMBED_RUN_SOAK=1` so it does not run in default CI.
"""
from __future__ import annotations

import os
import socket
import struct
import threading
import time

import pytest

from tests.quic.conftest import process_rss_kb, scrape_metric


pytestmark = [pytest.mark.soak]


def _make_chnk(stream_id: bytes, payload: bytes) -> bytes:
    hdr = struct.pack(">III", 0, 1, len(payload))
    return b"CHNK" + stream_id + hdr + payload


def _send_loop(target_port: int, stop: threading.Event, interval_s: float = 0.001) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pkt = _make_chnk(b"S" * 16, b"y" * 800)
    while not stop.is_set():
        s.sendto(pkt, ("127.0.0.1", target_port))
        time.sleep(interval_s)
    s.close()


def _drain_loop(listener: socket.socket, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            listener.recvfrom(65535)
        except socket.timeout:
            continue


@pytest.mark.skipif(os.getenv("STREAMBED_RUN_SOAK") != "1", reason="set STREAMBED_RUN_SOAK=1 to run")
def test_sidecar_rss_stable(sidecar_pair_factory):
    duration = int(os.getenv("STREAMBED_SOAK_DURATION_SECS", "30"))
    sample_interval = 5
    warmup_secs = 10  # exclude the first N seconds from slope calc

    pair = sidecar_pair_factory(edge_count=1)
    server = pair["server"]
    edge = pair["edges"][0]
    ports = pair["ports"]

    # Receiver at server's local UDP target.
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    listener.bind(("127.0.0.1", ports["server_local_udp"]))
    listener.settimeout(0.5)

    stop = threading.Event()
    drain = threading.Thread(target=_drain_loop, args=(listener, stop), daemon=True)
    drain.start()

    # Let handshake settle.
    time.sleep(2.0)

    sender = threading.Thread(
        target=_send_loop,
        args=(ports["edges_udp"][0], stop, 0.001),
        daemon=True,
    )
    sender.start()

    samples: list[tuple[float, int, int, float]] = []  # (t, server_rss, edge_rss, datagrams_sent)
    edge_metrics_url = f"http://127.0.0.1:{ports['edges_metrics'][0]}/metrics"

    t0 = time.monotonic()
    while time.monotonic() - t0 < duration:
        time.sleep(sample_interval)
        srv_rss = process_rss_kb(server.pid())
        edge_rss = process_rss_kb(edge.pid())
        try:
            dg = scrape_metric(edge_metrics_url, "streambed_sidecar_datagrams_sent")
        except Exception:
            dg = 0.0
        samples.append((time.monotonic() - t0, srv_rss, edge_rss, dg))

    stop.set()
    sender.join(timeout=2)
    drain.join(timeout=2)
    listener.close()

    # Assert traffic actually flowed.
    final_dg = samples[-1][3] if samples else 0
    assert final_dg > 100, f"sidecar did not send sustained traffic: datagrams_sent={final_dg}"

    # Slope check on RSS samples after warmup. KB / second.
    post_warmup = [(t, s, e) for (t, s, e, _) in samples if t >= warmup_secs]
    if len(post_warmup) < 2:
        pytest.skip("not enough post-warmup samples; raise STREAMBED_SOAK_DURATION_SECS")

    def _slope(series: list[tuple[float, int]]) -> float:
        n = len(series)
        sx = sum(t for t, _ in series)
        sy = sum(v for _, v in series)
        sxx = sum(t * t for t, _ in series)
        sxy = sum(t * v for t, v in series)
        denom = n * sxx - sx * sx
        return (n * sxy - sx * sy) / denom if denom else 0.0

    srv_slope = _slope([(t, s) for (t, s, _) in post_warmup])
    edge_slope = _slope([(t, e) for (t, _, e) in post_warmup])

    # Allow up to 50 KB/s growth — generous for a 30s window with allocator noise.
    # For a true 15-min soak, override with STREAMBED_SOAK_RSS_SLOPE_KBPS_MAX.
    max_slope = float(os.getenv("STREAMBED_SOAK_RSS_SLOPE_KBPS_MAX", "50"))
    print(f"[Soak] samples: {samples}")
    print(f"[Soak] server slope: {srv_slope:.2f} KB/s, edge slope: {edge_slope:.2f} KB/s, max={max_slope}")

    assert srv_slope < max_slope, f"server RSS growing too fast: {srv_slope:.1f} KB/s"
    assert edge_slope < max_slope, f"edge RSS growing too fast: {edge_slope:.1f} KB/s"

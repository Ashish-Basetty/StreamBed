"""Experiment harness: poll per-sidecar /metrics + advisor logs and stream
the result into a CSV time-series.

Sidecar metrics are scraped via `docker exec <sidecar> wget -qO- localhost:9100/metrics`
so we don't need to publish a metrics port to the host. The advisor's
`total_advised=` counter is parsed from its inference-container logs.
"""

from __future__ import annotations

import asyncio
import csv
import re
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Lines of the form:
#   streambed_sidecar_bytes_sent{tag="CHNK"} 12345
_METRIC_RE = re.compile(
    r'^streambed_sidecar_bytes_(sent|received)\{tag="([A-Z]{4})"\}\s+(\d+)'
)
# Advisor server emits e.g.  total_advised=42 (cadence_n=5)
_ADVISED_RE = re.compile(r"total_advised=(\d+)")
_CADENCE_RE = re.compile(r"cadence_n=(\d+)")


@dataclass
class MetricsSample:
    t_s: float
    chnk_bytes_sent: int = 0
    embd_bytes_sent: int = 0
    cstr_bytes_recv: int = 0
    cstl_bytes_sent: int = 0
    total_advised: int = 0
    cadence_n: int = 0
    throttle_bps: float = 0.0
    loss_pct: float = 0.0
    extra_latency_ms: float = 0.0


def _docker_exec(container: str, cmd: list[str], timeout: float = 5.0) -> str:
    """Run argv inside `container` and return stdout. Empty on error."""
    try:
        result = subprocess.run(
            ["docker", "exec", container, *cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return ""
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _docker_logs_tail(container: str, n: int = 200) -> str:
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(n), container],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        return (result.stdout or "") + (result.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


_SCRAPE_PY = (
    "import urllib.request,sys;"
    "sys.stdout.write(urllib.request.urlopen(sys.argv[1],timeout=2).read().decode())"
)


def _scrape_sidecar(scrape_via: str, sidecar_dns: str) -> dict[tuple[str, str], int]:
    """Return {(sent|received, tag): bytes}.

    Scrapes via the co-located daemon container (which lives on the sidecar's
    device network and has Python). The sidecar image is FROM scratch, so we
    can't exec a shell inside it.
    """
    body = _docker_exec(
        scrape_via,
        ["python", "-c", _SCRAPE_PY, f"http://{sidecar_dns}:9100/metrics"],
    )
    out: dict[tuple[str, str], int] = {}
    if not body:
        return out
    for line in body.splitlines():
        m = _METRIC_RE.match(line)
        if not m:
            continue
        direction, tag, value = m.group(1), m.group(2), int(m.group(3))
        out[(direction, tag)] = value
    return out


def _scrape_advisor(advisor_container: str) -> tuple[int, int]:
    """Pull (total_advised, cadence_n) from the latest matching log line."""
    logs = _docker_logs_tail(advisor_container, n=200)
    advised = 0
    cadence = 0
    # Iterate in order so the most recent line wins.
    for line in logs.splitlines():
        m = _ADVISED_RE.search(line)
        if m:
            advised = int(m.group(1))
        m = _CADENCE_RE.search(line)
        if m:
            cadence = int(m.group(1))
    return advised, cadence


@dataclass
class MetricsPoller:
    edge_sidecar: str
    server_sidecar: str
    advisor_container: str
    edge_daemon: str       # exec target for edge sidecar scraping
    server_daemon: str     # exec target for server sidecar scraping
    proxy_schedule: list[dict] = field(default_factory=list)
    cadence_schedule: list[dict] = field(default_factory=list)
    poll_interval_s: float = 1.0
    started_at: float = field(default_factory=time.monotonic)
    samples: list[MetricsSample] = field(default_factory=list)
    _task: asyncio.Task | None = None

    def _proxy_state_at(self, t_s: float) -> tuple[float, float, float]:
        active = None
        for seg in self.proxy_schedule:
            if t_s >= float(seg["at_s"]):
                active = seg
            else:
                break
        if active is None:
            return 0.0, 0.0, 0.0
        return (
            float(active.get("bps", 0)),
            float(active.get("loss_pct", 0.0)),
            float(active.get("extra_latency_ms", 0.0)),
        )

    async def _loop(self) -> None:
        while True:
            t = time.monotonic() - self.started_at
            edge = _scrape_sidecar(self.edge_daemon, self.edge_sidecar)
            # Server sidecar scrape skipped: CSTR (advice) is tracked on the
            # edge's `received` counter since it travels server→edge on the
            # control stream. Keep server_sidecar/server_daemon fields wired
            # so adding server-side recv stats later is a one-line change.
            advised, cadence = _scrape_advisor(self.advisor_container)
            bps, loss, lat = self._proxy_state_at(t)
            self.samples.append(MetricsSample(
                t_s=round(t, 2),
                chnk_bytes_sent=edge.get(("sent", "CHNK"), 0),
                embd_bytes_sent=edge.get(("sent", "EMBD"), 0),
                cstl_bytes_sent=edge.get(("sent", "CSTL"), 0),
                # CSTR (advice) flows server→edge over the control stream, so
                # the edge sidecar is the side that records it on receive.
                cstr_bytes_recv=edge.get(("received", "CSTR"), 0),
                total_advised=advised,
                cadence_n=cadence,
                throttle_bps=bps,
                loss_pct=loss,
                extra_latency_ms=lat,
            ))
            await asyncio.sleep(self.poll_interval_s)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        cols = [
            "t_s", "throttle_bps", "loss_pct", "extra_latency_ms", "cadence_n",
            "chnk_bytes_sent", "embd_bytes_sent", "cstl_bytes_sent",
            "cstr_bytes_recv", "total_advised",
            "embd_rate_hz", "chnk_rate_hz", "advice_rate_hz",
        ]
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            prev: MetricsSample | None = None
            for s in self.samples:
                if prev is None:
                    embd_rate = chnk_rate = adv_rate = 0.0
                else:
                    dt = max(s.t_s - prev.t_s, 1e-6)
                    embd_rate = (s.embd_bytes_sent - prev.embd_bytes_sent) / dt
                    chnk_rate = (s.chnk_bytes_sent - prev.chnk_bytes_sent) / dt
                    adv_rate = (s.total_advised - prev.total_advised) / dt
                w.writerow({
                    "t_s": s.t_s,
                    "throttle_bps": s.throttle_bps,
                    "loss_pct": s.loss_pct,
                    "extra_latency_ms": s.extra_latency_ms,
                    "cadence_n": s.cadence_n,
                    "chnk_bytes_sent": s.chnk_bytes_sent,
                    "embd_bytes_sent": s.embd_bytes_sent,
                    "cstl_bytes_sent": s.cstl_bytes_sent,
                    "cstr_bytes_recv": s.cstr_bytes_recv,
                    "total_advised": s.total_advised,
                    "embd_rate_hz": round(embd_rate, 2),
                    "chnk_rate_hz": round(chnk_rate, 2),
                    "advice_rate_hz": round(adv_rate, 2),
                })
                prev = s


def write_schedule(path: Path, segments: Iterable[dict]) -> None:
    """Write a varying-proxy schedule.yml. Segments must be sorted by at_s."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(list(segments), f, sort_keys=False)


def write_target(path: Path, host: str, port: int) -> None:
    """Tell the varying proxy where to forward to."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump({"target_host": host, "target_port": port}, f)


def load_schedule(path: Path) -> list[dict]:
    with path.open() as f:
        return list(yaml.safe_load(f) or [])

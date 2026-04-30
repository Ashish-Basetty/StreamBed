"""Shared fixtures for QUIC sidecar integration tests.

The Go binary is built once per pytest session and cached in `sidecar/bin/`.
Tests that need the binary use the `sidecar_binary` fixture; tests skip
gracefully when `go` is not on PATH.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SIDECAR_DIR = _REPO_ROOT / "sidecar"
_BINARY_PATH = _SIDECAR_DIR / "bin" / "streambed-quic-sidecar"


@pytest.fixture(scope="session")
def sidecar_binary() -> Path:
    """Build the Go sidecar binary once per session. Skip if `go` unavailable."""
    if shutil.which("go") is None:
        pytest.skip("go toolchain not on PATH")
    _BINARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["go", "build", "-o", str(_BINARY_PATH), "./cmd/streambed-quic-sidecar"],
        cwd=str(_SIDECAR_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"go build failed:\n{result.stderr}")
    return _BINARY_PATH


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SidecarProcess:
    """Subprocess wrapper for one sidecar role. Captures stdout/stderr to a tempfile."""

    def __init__(
        self,
        binary: Path,
        *,
        role: str,
        env: dict[str, str],
        log_path: Path | None = None,
    ):
        self.binary = binary
        self.role = role
        self.env_overrides = env
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self._log_fh = None

    def start(self) -> None:
        env = os.environ.copy()
        env.update(self.env_overrides)
        env["SIDECAR_ROLE"] = self.role
        if self.log_path is not None:
            self._log_fh = open(self.log_path, "wb")
            stdout = self._log_fh
            stderr = subprocess.STDOUT
        else:
            stdout = subprocess.DEVNULL
            stderr = subprocess.DEVNULL
        self.proc = subprocess.Popen(
            [str(self.binary)],
            env=env,
            stdout=stdout,
            stderr=stderr,
        )

    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 3.0) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None


def wait_for_metrics(metrics_url: str, timeout: float = 5.0) -> None:
    """Block until the /metrics endpoint responds, then return."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(metrics_url, timeout=0.5)
            if r.status_code == 200:
                return
        except Exception as e:
            last_exc = e
        time.sleep(0.05)
    raise TimeoutError(f"metrics endpoint {metrics_url} not ready: {last_exc}")


def scrape_metric(metrics_url: str, name: str) -> float:
    """Pull one named counter/gauge from a Prometheus text response."""
    r = httpx.get(metrics_url, timeout=1.0)
    r.raise_for_status()
    for line in r.text.splitlines():
        if line.startswith(name + " "):
            return float(line.split()[1])
    raise KeyError(name)


def process_rss_kb(pid: int) -> int:
    """Resident set size in KB via `ps`. Works on macOS and Linux."""
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"ps failed for pid {pid}: {out.stderr}")
    return int(out.stdout.strip())


@pytest.fixture
def free_port():
    return _free_port


@pytest.fixture
def free_udp_port():
    return _free_udp_port


@pytest.fixture
def sidecar_pair_factory(sidecar_binary, tmp_path):
    """Returns a callable that spawns 1 server + N edges; cleans up at teardown.

    Each call returns a dict with `server`, `edges` (list), and `ports`.
    `ports.edges[i]` is the local UDP port to send into for edge i.
    """
    spawned: list[SidecarProcess] = []

    def _factory(edge_count: int = 1) -> dict:
        server_quic = _free_udp_port()
        server_metrics = _free_port()
        server_local_udp = _free_udp_port()
        daemon_feedback = _free_udp_port()

        server = SidecarProcess(
            sidecar_binary,
            role="server",
            env={
                "QUIC_BIND": f"127.0.0.1:{server_quic}",
                "LOCAL_SERVER_UDP": f"127.0.0.1:{server_local_udp}",
                "METRICS_ADDR": f"127.0.0.1:{server_metrics}",
            },
            log_path=tmp_path / "server.log",
        )
        server.start()
        wait_for_metrics(f"http://127.0.0.1:{server_metrics}/metrics")
        spawned.append(server)

        edges = []
        edge_udp_ports = []
        edge_metrics_ports = []
        for i in range(edge_count):
            u_port = _free_udp_port()
            m_port = _free_port()
            e = SidecarProcess(
                sidecar_binary,
                role="edge",
                env={
                    "PEER_ADDRESS": f"127.0.0.1:{server_quic}",
                    "LOCAL_UDP_BIND": f"127.0.0.1:{u_port}",
                    "DAEMON_ADDRESS": f"127.0.0.1:{daemon_feedback}",
                    "METRICS_ADDR": f"127.0.0.1:{m_port}",
                },
                log_path=tmp_path / f"edge-{i}.log",
            )
            e.start()
            wait_for_metrics(f"http://127.0.0.1:{m_port}/metrics")
            spawned.append(e)
            edges.append(e)
            edge_udp_ports.append(u_port)
            edge_metrics_ports.append(m_port)

        return {
            "server": server,
            "edges": edges,
            "ports": {
                "server_quic": server_quic,
                "server_metrics": server_metrics,
                "server_local_udp": server_local_udp,
                "edges_udp": edge_udp_ports,
                "edges_metrics": edge_metrics_ports,
            },
        }

    yield _factory

    for p in spawned:
        p.stop()

"""Shared fixtures for QUIC sidecar integration tests.

The Go binary is built once per pytest session and cached in `sidecar/bin/`.
Tests that need the binary use the `sidecar_binary` fixture; tests skip
gracefully when `go` is not on PATH.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

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


class MockDaemonServer:
    """Minimal HTTP server that serves GET /stream-target for a sidecar under test.

    target_ip / quic_port can be updated at runtime to simulate rerouting.
    quic_port=0 omits the field so the sidecar falls back to PEER_QUIC_PORT.
    """

    def __init__(self, target_ip: str, quic_port: int = 0):
        self._target_ip = target_ip
        self._quic_port = quic_port
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def set_target(self, target_ip: str, quic_port: int = 0) -> None:
        with self._lock:
            self._target_ip = target_ip
            self._quic_port = quic_port

    def start(self) -> int:
        """Start serving; returns the bound port."""
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/stream-target":
                    with parent._lock:
                        payload: dict = {"target_ip": parent._target_ip, "target_port": 0}
                        if parent._quic_port:
                            payload["quic_port"] = parent._quic_port
                        body = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):  # silence access logs in test output
                pass

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return port

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()


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

    `enable_reverse=True` opens the optional reverse-path UDP listener on the
    server (`SERVER_REVERSE_UDP_BIND`) and the optional UDP forward target on
    each edge (`LOCAL_RECV_UDP_TARGET` -> a freshly-bound socket per edge),
    so server-originated app data flows back as control frames and lands in
    a per-edge listener that the test can read.
    """
    spawned: list[SidecarProcess] = []
    mock_daemons: list[MockDaemonServer] = []
    sockets: list[socket.socket] = []

    def _factory(edge_count: int = 1, enable_reverse: bool = False) -> dict:
        server_quic = _free_udp_port()
        server_metrics = _free_port()
        server_local_udp = _free_udp_port()

        server_env = {
            "QUIC_BIND": f"127.0.0.1:{server_quic}",
            "LOCAL_SERVER_UDP": f"127.0.0.1:{server_local_udp}",
            "METRICS_ADDR": f"127.0.0.1:{server_metrics}",
        }
        server_reverse_port = None
        if enable_reverse:
            server_reverse_port = _free_udp_port()
            server_env["SERVER_REVERSE_UDP_BIND"] = f"127.0.0.1:{server_reverse_port}"

        server = SidecarProcess(
            sidecar_binary,
            role="server",
            env=server_env,
            log_path=tmp_path / "server.log",
        )
        server.start()
        wait_for_metrics(f"http://127.0.0.1:{server_metrics}/metrics")
        spawned.append(server)

        edges = []
        edge_udp_ports = []
        edge_metrics_ports = []
        edge_recv_socks: list[socket.socket | None] = []
        edge_mock_daemons: list[MockDaemonServer] = []
        for i in range(edge_count):
            u_port = _free_udp_port()
            m_port = _free_port()
            # Start a mock daemon HTTP server serving /stream-target so the
            # sidecar can discover its peer without PEER_ADDRESS env var.
            # quic_port tells the sidecar the exact QUIC port to dial.
            mock = MockDaemonServer(target_ip="127.0.0.1", quic_port=server_quic)
            daemon_port = mock.start()
            mock_daemons.append(mock)
            edge_mock_daemons.append(mock)
            edge_env = {
                "DAEMON_URL": f"http://127.0.0.1:{daemon_port}",
                "LOCAL_UDP_BIND": f"127.0.0.1:{u_port}",
                "METRICS_ADDR": f"127.0.0.1:{m_port}",
            }
            recv_sock: socket.socket | None = None
            if enable_reverse:
                # Bind the listener BEFORE starting the sidecar so the sidecar's
                # first UDP write doesn't ICMP-bounce.
                recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                recv_sock.bind(("127.0.0.1", 0))
                recv_sock.settimeout(2.0)
                sockets.append(recv_sock)
                recv_port = recv_sock.getsockname()[1]
                edge_env["LOCAL_RECV_UDP_TARGET"] = f"127.0.0.1:{recv_port}"
            edge_recv_socks.append(recv_sock)
            e = SidecarProcess(
                sidecar_binary,
                role="edge",
                env=edge_env,
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
            "mock_daemons": edge_mock_daemons,
            "ports": {
                "server_quic": server_quic,
                "server_metrics": server_metrics,
                "server_local_udp": server_local_udp,
                "server_reverse_udp": server_reverse_port,
                "edges_udp": edge_udp_ports,
                "edges_metrics": edge_metrics_ports,
            },
            "edge_recv_socks": edge_recv_socks,
        }

    yield _factory

    for p in spawned:
        p.stop()
    for m in mock_daemons:
        m.stop()
    for s in sockets:
        try:
            s.close()
        except Exception:
            pass

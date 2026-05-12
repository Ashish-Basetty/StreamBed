"""Advisor Phase D Docker smoke tests."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.deploy_utils import _wait_for_controller, _wait_for_daemons, delete_device
from tests.docker_utils import DockerComposeManager, reap_streambed_runtime_containers

pytestmark = [pytest.mark.integration, pytest.mark.integration_docker]

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEACHER = _REPO_ROOT / "experiments" / "advisor" / "checkpoints" / "teacher.zip"
_STUDENT = _REPO_ROOT / "experiments" / "advisor" / "checkpoints" / "students" / "shared_h4.pt"
_CONTROLLER_DB = _REPO_ROOT / "control-plane" / "data" / "controller.db"
_COMPOSE_FILES = ["docker-compose.yml", "experiments/advisor/docker-compose.advisor.yml"]
_ADVISOR_EDGE_IMAGE = "ashishbasetty/streambed-advisor-edge:latest"
_ADVISOR_SERVER_IMAGE = "ashishbasetty/streambed-advisor-server:latest"
_CONTROLLER_URL = "http://localhost:8080"
_EDGE_HOST_PORT = 9100
_EDGE_SIDECAR = "streambed-default-edge-001-sidecar"
_SERVER_SIDECAR = "streambed-default-server-001-sidecar"
_EDGE_DAEMON_URL = "http://localhost:9090"


def _run(cmd: list[str], *, timeout: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _assert_success(result: subprocess.CompletedProcess, label: str) -> None:
    assert result.returncode == 0, (
        f"{label} failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def _remove_controller_db() -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(_CONTROLLER_DB) + suffix).unlink(missing_ok=True)


def _docker_output(args: list[str], *, timeout: int = 30) -> str:
    result = _run(["docker", *args], timeout=timeout)
    _assert_success(result, f"docker {' '.join(args)}")
    return result.stdout


def _container_name(prefix: str) -> str:
    names = _docker_output(["ps", "-a", "--format", "{{.Names}}"]).splitlines()
    matches = [
        name
        for name in names
        if name.startswith(prefix) and name not in {_EDGE_SIDECAR, _SERVER_SIDECAR}
    ]
    assert matches, f"no container found with prefix {prefix!r}; containers={names}"
    return matches[0]


def _docker_logs(container_name: str) -> str:
    return _docker_output(["logs", container_name, "--tail", "240"])


def _assert_sidecar_metrics(logs: str, *, role: str) -> None:
    metric_lines = [line for line in logs.splitlines() if f"metrics role={role}" in line]
    assert metric_lines, f"no metrics lines found for {role} sidecar\n{logs}"

    counters: dict[str, int] = {}
    for line in metric_lines:
        for key, value in re.findall(r"\b(dg_sent|dg_recv|bytes_sent|bytes_recv|stream_sent|stream_recv)=(\d+)", line):
            counters[key] = max(counters.get(key, 0), int(value))

    assert any(value > 0 for value in counters.values()), (
        f"{role} sidecar metrics never moved; counters={counters}\n{logs}"
    )


def _assert_server_advised(logs: str) -> None:
    counts = [int(value) for value in re.findall(r"total_advised=(\d+)", logs)]
    assert counts and max(counts) > 0, f"advisor server never produced advice\n{logs}"


def _set_stream_target() -> None:
    with httpx.Client(timeout=10) as client:
        resp = client.put(
            f"{_EDGE_DAEMON_URL}/stream-target",
            json={"target_ip": _SERVER_SIDECAR, "target_port": 9000},
        )
        resp.raise_for_status()


def _wait_for_log(container_name: str, needle: str, *, timeout_s: float = 45.0) -> str:
    deadline = time.time() + timeout_s
    last_logs = ""
    while time.time() < deadline:
        last_logs = _docker_logs(container_name)
        if needle in last_logs:
            return last_logs
        time.sleep(1)
    raise AssertionError(f"{needle!r} not found in logs for {container_name}\n{last_logs}")


def test_advisor_phase_d_docker_smoke_one_episode(tmp_path: Path) -> None:
    """Run one episode through controller-deployed advisor containers and sidecars."""
    if not _TEACHER.exists() or not _STUDENT.exists():
        pytest.skip("advisor checkpoints are not available")

    out = tmp_path / "advisor_phase_d_1ep.json"
    manager = DockerComposeManager(
        compose_files=_COMPOSE_FILES,
        project_name="streambed",
    )

    try:
        reap_streambed_runtime_containers()
        manager.down_services()
        _remove_controller_db()

        build = _run(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.yml",
                "-f",
                "experiments/advisor/docker-compose.advisor.yml",
                "build",
                "advisor-edge",
                "advisor-server",
            ],
            timeout=900,
        )
        _assert_success(build, "advisor image build")

        manager.up_services(
            services=["controller", "router", "daemon-edge1", "daemon-server1"],
            flags={"build": True, "force_recreate": True},
        )
        _wait_for_controller(_CONTROLLER_URL)
        _wait_for_daemons(daemon_ports=[9090, 9093])

        deploy = _run(
            ["bash", "experiments/advisor/scripts/deploy_advisor.sh"],
            timeout=300,
            env={
                **os.environ,
                "CONTROLLER_URL": _CONTROLLER_URL,
                "ADVISOR_EDGE_IMAGE": _ADVISOR_EDGE_IMAGE,
                "ADVISOR_SERVER_IMAGE": _ADVISOR_SERVER_IMAGE,
            },
        )
        _assert_success(deploy, "advisor deploy")
        _set_stream_target()
        _wait_for_log(_EDGE_SIDECAR, "QUIC connected")

        frame_gen = _run(
            [
                sys.executable,
                "-m",
                "experiments.advisor.host.frame_gen",
                "--episodes",
                "1",
                "--edge-host",
                "127.0.0.1",
                "--edge-port",
                str(_EDGE_HOST_PORT),
                "--out",
                str(out),
            ],
            timeout=300,
        )
        _assert_success(frame_gen, "advisor frame_gen")

        # Sidecar metrics are emitted periodically; wait for one tick after traffic.
        time.sleep(11)
        edge_container = _container_name("streambed-default-edge-001-")
        server_container = _container_name("streambed-default-server-001-")
        edge_logs = _docker_logs(edge_container)
        server_logs = _docker_logs(server_container)
        edge_sidecar_logs = _docker_logs(_EDGE_SIDECAR)
        server_sidecar_logs = _docker_logs(_SERVER_SIDECAR)
    finally:
        for device_id in ("server-001", "edge-001"):
            try:
                delete_device(device_id, controller_url=_CONTROLLER_URL)
            except Exception:
                pass
        manager.down_services()
        reap_streambed_runtime_containers()

    summary = json.loads(out.read_text())
    assert summary["episodes"] == 1
    assert summary["edge_host"] == "127.0.0.1"
    assert summary["edge_port"] == _EDGE_HOST_PORT
    assert summary["avg_advice_pct"] > 0
    assert "advice received" in edge_logs
    _assert_server_advised(server_logs)
    assert "no such host" not in edge_sidecar_logs
    _assert_sidecar_metrics(edge_sidecar_logs, role="edge")
    _assert_sidecar_metrics(server_sidecar_logs, role="server")

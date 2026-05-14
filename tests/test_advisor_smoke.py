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


def _remove_daemon_state() -> None:
    """Wipe per-daemon persisted files. The host-mounted ./daemon-data/edgeN/
    volume survives `compose down`, so a previous run's stream-target.json can
    feed a stale target into the new edge sidecar's first poll before the test
    issues its PUT — producing "no such host" lines on dial. Same idea as
    _remove_controller_db, just for the daemon side."""
    daemon_data = _REPO_ROOT / "daemon-data"
    if not daemon_data.exists():
        return
    for pattern in ("*/stream-target.json", "*/deployed.json"):
        for f in daemon_data.glob(pattern):
            f.unlink(missing_ok=True)


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


def _docker_logs(container_name: str, *, tail: int | None = 240) -> str:
    """Fetch container logs. Default tail=240 keeps asserts cheap; use a large tail
    when polling for a needle so success lines are not dropped off the tail.

    Merge CLI stdout+stderr: some Docker Desktop builds send the log stream or
    warnings on stderr; capture_output-only stdout looked 'empty' even when
    the container was writing logs."""
    args: list[str]
    if tail is None:
        args = ["logs", container_name]
    else:
        args = ["logs", container_name, "--tail", str(tail)]
    result = _run(["docker", *args], timeout=30)
    _assert_success(result, f"docker {' '.join(args)}")
    return (result.stdout or "") + (result.stderr or "")


def _docker_inspect_log_hints(container_name: str) -> str:
    """Best-effort context when `docker logs` is empty (driver, tty, log path)."""
    fmt = (
        "status={{.State.Status}} running={{.State.Running}} exit={{.State.ExitCode}} "
        "oom={{.State.OOMKilled}} restart={{.RestartCount}} "
        "log_driver={{.HostConfig.LogConfig.Type}} log_path={{.LogPath}} tty={{.Config.Tty}}"
    )
    r = _run(
        ["docker", "inspect", "--format", fmt, container_name],
        timeout=30,
    )
    if r.returncode != 0:
        return f"docker inspect failed (exit {r.returncode}): {r.stderr or r.stdout}".strip()
    return (r.stdout or "").strip()


def _assert_logs_non_empty(
    container_name: str, logs: str, *, what: str = "check",
) -> None:
    """Fail fast if `docker logs` is empty — often wrong DOCKER_HOST / context vs compose."""
    assert logs.strip(), (
        f"empty docker logs for container {container_name!r} ({what}). "
        "If compose is up elsewhere, align DOCKER_HOST / `docker context` with this test host."
    )


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


def _print_docker_ps_before_log_wait() -> None:
    """Show which containers this pytest process's `docker` CLI sees (debug context/network)."""
    result = _run(["docker", "ps"], timeout=30)
    blob = result.stdout
    if result.stderr:
        blob += result.stderr
    if result.returncode != 0:
        blob = f"docker ps failed (exit {result.returncode})\n{blob}"
    print(
        "\n[advisor_smoke] docker ps (before edge sidecar log wait):\n" + blob.rstrip() + "\n",
        file=sys.stderr,
        flush=True,
    )


def _wait_for_log(container_name: str, needle: str, *, timeout_s: float = 45.0) -> str:
    deadline = time.time() + timeout_s
    last_logs = ""
    saw_nonempty = False
    while time.time() < deadline:
        # Full logs until connect — usually small; avoids missing an early line if tail is too small.
        last_logs = _docker_logs(container_name, tail=None)
        if last_logs.strip():
            saw_nonempty = True
        if needle in last_logs:
            _assert_logs_non_empty(container_name, last_logs, what="wait_for_log match")
            return last_logs
        time.sleep(1)
    detail = (
        f"{needle!r} not found in logs for {container_name} after {timeout_s}s.\n"
    )
    if not saw_nonempty:
        detail += (
            "docker logs were always empty for this name — often a different Docker "
            "context/network than the one running compose (check `docker context show` "
            "and that this host sees the container in `docker ps`).\n"
        )
        detail += f"inspect: {_docker_inspect_log_hints(container_name)}\n"
    detail += last_logs
    raise AssertionError(detail)


def test_advisor_phase_d_docker_smoke_one_episode(tmp_path: Path, keep_docker: bool) -> None:
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
        _remove_daemon_state()

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
        _assert_logs_non_empty(edge_container, edge_logs, what="edge inference")
        _assert_logs_non_empty(server_container, server_logs, what="server inference")
        _assert_logs_non_empty(_EDGE_SIDECAR, edge_sidecar_logs, what="edge sidecar")
        _assert_logs_non_empty(_SERVER_SIDECAR, server_sidecar_logs, what="server sidecar")
    finally:
        if keep_docker:
            print("\n[pytest --keep-docker] Skipping device delete, compose down, and reap.")
        else:
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

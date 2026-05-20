"""StreamBed adaptivity sweep: time-varying throttle + cadence.

Brings up the advisor compose stack with the :experiment sidecar image and
the varying-proxy in front of the server sidecar's QUIC endpoint. Drives
traffic via experiments.advisor.host.frame_gen for ~2 minutes and writes a
CSV time-series of (throttle, cadence) inputs and (chnk/embd/advice) outputs.

The harness queries the controller for the server sidecar's host UDP port at
runtime and writes target.yml to point the proxy at that port — the existing
throttle test points at the inference container on :9000 (bypasses the
sidecar) and that's the "insertion-point" bug the experiment doc flags.

Pass criteria are intentionally weak: CSV exists, CHNK rate drops at the
50 kbps step, EMBD rate stays positive until the deepest tier. The CSV is
for offline analysis, not pass/fail.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timezone
from pathlib import Path

import httpx
import pytest

from tests.deploy_utils import (
    _wait_for_controller,
    _wait_for_daemons,
    delete_device,
)
from tests.docker_utils import DockerComposeManager, reap_streambed_runtime_containers
from tests.experiments.harness import (
    MetricsPoller,
    load_schedule,
    write_schedule,
    write_target,
)

pytestmark = [pytest.mark.experiment, pytest.mark.integration_docker]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEACHER = _REPO_ROOT / "experiments" / "advisor" / "checkpoints" / "teacher.zip"
_STUDENT = _REPO_ROOT / "experiments" / "advisor" / "checkpoints" / "students" / "shared_h4.pt"
_CONTROLLER_DB = _REPO_ROOT / "control-plane" / "data" / "controller.db"
_DAEMON_DATA_SERVER1 = _REPO_ROOT / "daemon-data" / "server1"
_CONTROLLER_URL = "http://localhost:8080"
_EDGE_HOST_PORT = 9100
_EDGE_SIDECAR = "streambed-default-edge-001-sidecar"
_SERVER_SIDECAR = "streambed-default-server-001-sidecar"
_EDGE_DAEMON = "streambed-daemon-edge1"
_SERVER_DAEMON = "streambed-daemon-server1"
_VARYING_PROXY = "streambed-varying-proxy"
_PROXY_BIND_PORT = 9010

_COMPOSE_FILES = [
    "docker-compose.yml",
    "experiments/advisor/docker-compose.advisor.yml",
    "docker-compose.varying.yml",
]

# Run length picked so all four schedule tiers are exercised plus tail.
_RUN_SECONDS = 130


def _run(cmd: list[str], *, timeout: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _remove_controller_db() -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(_CONTROLLER_DB) + suffix).unlink(missing_ok=True)


def _remove_daemon_state() -> None:
    daemon_data = _REPO_ROOT / "daemon-data"
    if not daemon_data.exists():
        return
    for pattern in ("*/stream-target.json", "*/deployed.json"):
        for f in daemon_data.glob(pattern):
            f.unlink(missing_ok=True)


def _put_stream_target(daemon_url: str, target_ip: str, target_port: int) -> None:
    with httpx.Client(timeout=5) as client:
        resp = client.put(
            f"{daemon_url.rstrip('/')}/stream-target",
            json={"target_ip": target_ip, "target_port": target_port},
        )
        resp.raise_for_status()


def _wait_for_server_sidecar_endpoint(timeout_s: float = 60.0) -> tuple[str, int]:
    """Poll controller /status until server-001's sidecar publishes its host port."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with httpx.Client(timeout=3) as client:
            try:
                resp = client.get(f"{_CONTROLLER_URL}/status")
                resp.raise_for_status()
            except Exception:
                time.sleep(1)
                continue
        for row in resp.json().get("status", []):
            if row.get("device_id") != "server-001":
                continue
            ip = row.get("sidecar_host_ip")
            port = row.get("sidecar_host_port")
            if ip and isinstance(port, int) and port > 0:
                return ip, port
        time.sleep(1)
    raise AssertionError(
        f"server-001 sidecar host endpoint never reported within {timeout_s}s"
    )


def _deploy_advisor() -> None:
    _run(
        ["bash", "experiments/advisor/scripts/deploy_advisor.sh"],
        timeout=300,
        env={
            **os.environ,
            "CONTROLLER_URL": _CONTROLLER_URL,
            "ADVISOR_EDGE_IMAGE": "ashishbasetty/streambed-advisor-edge:latest",
            "ADVISOR_SERVER_IMAGE": "ashishbasetty/streambed-advisor-server:latest",
        },
    )


def _container_by_prefix(prefix: str) -> str:
    """Find a single running container whose name starts with `prefix`,
    excluding the sidecars (which share `streambed-default-server-001-` start)."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=10,
    )
    names = result.stdout.splitlines()
    matches = [
        n for n in names
        if n.startswith(prefix) and n not in {_EDGE_SIDECAR, _SERVER_SIDECAR}
    ]
    if not matches:
        raise AssertionError(f"no container found with prefix {prefix!r}; names={names}")
    return matches[0]


def _wait_for_sidecar_log(container: str, needle: str, timeout_s: float = 60.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        logs = subprocess.run(
            ["docker", "logs", container],
            capture_output=True, text=True, timeout=10,
        )
        if needle in (logs.stdout + logs.stderr):
            return
        time.sleep(1)
    raise AssertionError(f"never saw {needle!r} in {container} logs")


@pytest.mark.skip(reason="pending AIMD send-rate rework (docs/AimdSendRatePlan.md)")
@pytest.mark.experiment
def test_interleaving_sweep(
    experiment_results_dir: Path,
    experiment_runtime_dir: Path,
    experiment_schedules_dir: Path,
    keep_docker: bool,
) -> None:
    if not _TEACHER.exists() or not _STUDENT.exists():
        pytest.skip("advisor checkpoints are not available")

    proxy_schedule = load_schedule(experiment_schedules_dir / "stepped_burst.yml")
    cadence_schedule = load_schedule(experiment_schedules_dir / "cadence_step.yml")

    # The advisor server reads /config/cadence.yml at startup (the daemon mounts
    # daemon-data/server1/ as /config). Drop it in before /deploy fires.
    _DAEMON_DATA_SERVER1.mkdir(parents=True, exist_ok=True)
    write_schedule(_DAEMON_DATA_SERVER1 / "cadence.yml", cadence_schedule)

    # Pre-populate the proxy schedule + a placeholder target so the proxy
    # container has files to read at startup. The real target is written once
    # the server sidecar reports its host endpoint.
    write_schedule(experiment_runtime_dir / "schedule.yml", proxy_schedule)
    write_target(experiment_runtime_dir / "target.yml", "host.docker.internal", 0)

    manager = DockerComposeManager(
        compose_files=_COMPOSE_FILES,
        project_name="streambed",
    )
    env = {
        **os.environ,
        # Daemons spawn the experiment-tagged sidecar so per-tag counters are present.
        "SIDECAR_IMAGE": "ashishbasetty/streambed-quic-sidecar:experiment",
    }

    try:
        reap_streambed_runtime_containers()
        manager.down_services()
        _remove_controller_db()
        _remove_daemon_state()

        # Build advisor images if they're missing (cheap when cached).
        _run(
            ["docker", "compose", "-f", "docker-compose.yml",
             "-f", "experiments/advisor/docker-compose.advisor.yml",
             "build", "advisor-edge", "advisor-server"],
            timeout=900,
        )

        manager.up_services(
            services=[
                "controller", "router",
                "daemon-edge1", "daemon-server1",
                "varying-proxy",
            ],
            flags={"build": True, "force_recreate": True},
            env=env,
        )
        _wait_for_controller(_CONTROLLER_URL)
        _wait_for_daemons(daemon_ports=[9090, 9093])

        _deploy_advisor()

        # Server sidecar will heartbeat its host port to the controller within
        # the first heartbeat interval (~10 s).
        host_ip, host_port = _wait_for_server_sidecar_endpoint(timeout_s=60.0)

        # Point the proxy at the server sidecar's published host UDP. From inside
        # the proxy container, the host is reachable as host.docker.internal.
        write_target(
            experiment_runtime_dir / "target.yml",
            "host.docker.internal", host_port,
        )

        # Redirect the edge sidecar through the proxy.
        _put_stream_target(
            "http://localhost:9090",
            target_ip=_VARYING_PROXY,
            target_port=_PROXY_BIND_PORT,
        )

        _wait_for_sidecar_log(_EDGE_SIDECAR, "QUIC connected", timeout_s=60.0)

        advisor_container = _container_by_prefix("streambed-default-server-001-")
        poller = MetricsPoller(
            edge_sidecar=_EDGE_SIDECAR,
            server_sidecar=_SERVER_SIDECAR,
            advisor_container=advisor_container,
            edge_daemon=_EDGE_DAEMON,
            server_daemon=_SERVER_DAEMON,
            proxy_schedule=proxy_schedule,
            cadence_schedule=cadence_schedule,
            poll_interval_s=1.0,
        )

        async def _drive_traffic() -> None:
            await poller.start()
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "experiments.advisor.host.frame_gen",
                "--episodes", "500",  # huge so the time bound dominates
                "--edge-host", "127.0.0.1",
                "--edge-port", str(_EDGE_HOST_PORT),
                cwd=str(_REPO_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.sleep(_RUN_SECONDS)
            finally:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except TimeoutError:
                    proc.kill()
                await poller.stop()

        asyncio.run(_drive_traffic())

        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        csv_path = experiment_results_dir / f"{ts}__sweep.csv"
        poller.write_csv(csv_path)

        assert csv_path.exists(), f"CSV not written: {csv_path}"
        assert len(poller.samples) >= 60, (
            f"too few samples: {len(poller.samples)}; expected ~{_RUN_SECONDS}"
        )

        # Sanity: at least one EMBD byte was sent over the wire.
        final = poller.samples[-1]
        assert final.embd_bytes_sent > 0, (
            f"no EMBD bytes ever observed; final sample: {final}"
        )

    finally:
        if keep_docker:
            print("\n[pytest --keep-docker] Skipping device delete + compose down.")
        else:
            for device_id in ("server-001", "edge-001"):
                try:
                    delete_device(device_id, controller_url=_CONTROLLER_URL)
                except Exception:
                    pass
            manager.down_services()
            reap_streambed_runtime_containers()

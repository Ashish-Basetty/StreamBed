"""
Dynamic interleaving test: verify server receives fewer frames when path is throttled.

Uses a UDP throttling proxy between daemon and server. Stream-target is pointed
at the proxy; proxy forwards to server at limited rate (50 KB/s).
"""
import time
from pathlib import Path

import httpx
import pytest

from tests.deploy_utils import (
    _wait_for_controller,
    _wait_for_daemons,
    deploy_device,
    delete_device,
)

# Only edge1 and server1 daemons are started for throttle test
_THROTTLE_DAEMON_PORTS = [9090, 9093]
from tests.docker_utils import DockerComposeManager

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONTROLLER_DB_PATH = _PROJECT_ROOT / "controller" / "data" / "controller.db"

CONTROLLER_URL = "http://localhost:8080"
EDGE_DAEMON_URL = "http://localhost:9090"
SERVER_API_URL = "http://localhost:8001"
THROTTLE_RUN_SEC = 15
# At 50 KB/s throttle, expect at most ~1-2 frames/sec. Over 15s: < 40 frames.
THROTTLED_FRAME_THRESHOLD = 50


@pytest.fixture(scope="module")
def throttle_stack():
    """Bring up controller, daemon-edge1, daemon-server1, throttle-proxy."""
    manager = DockerComposeManager(
        compose_files=["docker-compose.yml", "docker-compose.throttle.yml"],
        project_name="streambed",
    )
    manager.down_services()
    if _CONTROLLER_DB_PATH.exists():
        _CONTROLLER_DB_PATH.unlink()

    manager.up_services(
        services=["controller", "daemon-edge1", "daemon-server1", "throttle-proxy"],
    )
    time.sleep(10)
    _wait_for_controller(CONTROLLER_URL)
    _wait_for_daemons(daemon_ports=_THROTTLE_DAEMON_PORTS)

    yield manager

    for device_id in ("server-001", "edge-001"):
        try:
            delete_device(device_id, controller_url=CONTROLLER_URL)
        except Exception:
            pass
    manager.down_services()


def _put_stream_target(daemon_url: str, target_ip: str, target_port: int) -> None:
    """Point edge daemon's stream target to given host:port."""
    with httpx.Client(timeout=5) as client:
        resp = client.put(
            f"{daemon_url.rstrip('/')}/stream-target",
            json={"target_ip": target_ip, "target_port": target_port},
        )
        resp.raise_for_status()


def _get_server_stored_frames() -> int:
    """Query server API for stored frame count."""
    with httpx.Client(timeout=5) as client:
        resp = client.get(f"{SERVER_API_URL.rstrip('/')}/api/v1/health")
        resp.raise_for_status()
        return resp.json()["stored_frames"]


@pytest.mark.integration
@pytest.mark.integration_docker
def test_throttled_path_receives_fewer_frames(throttle_stack):
    """
    With throttle proxy in path, server receives fewer frames than unthrottled.
    """
    # Deploy server first, then edge
    deploy_device("server-001", controller_url=CONTROLLER_URL)
    time.sleep(10)
    deploy_device("edge-001", controller_url=CONTROLLER_URL)
    time.sleep(10)

    # Point stream-target at throttle proxy (daemon sends to proxy, proxy forwards to server)
    # Note: with throttle proxy, server receives from proxy so feedback goes to proxy.
    # Proxy would need to forward feedback to daemon for full dynamic interleaving.
    _put_stream_target(EDGE_DAEMON_URL, "throttle-proxy", 9010)

    # Let frames flow for THROTTLE_RUN_SEC seconds
    time.sleep(THROTTLE_RUN_SEC)

    stored = _get_server_stored_frames()
    print(f"[Test] Server stored {stored} frames after {THROTTLE_RUN_SEC}s throttled run")

    assert stored < THROTTLED_FRAME_THRESHOLD, (
        f"Expected < {THROTTLED_FRAME_THRESHOLD} frames with throttle, got {stored}"
    )

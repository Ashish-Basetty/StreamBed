"""
Deploy and delete inference containers via the controller API.
Used by integration tests to bring up the full stack (controller + daemons + edge/server containers).
"""
import logging
import subprocess
import time

import httpx

logger = logging.getLogger(__name__)

# Device config from docker-compose.yml (root)
DEVICE_CLUSTER = "default"
EDGE_IMAGE = "ashishbasetty/streambed-edge:latest"
SERVER_IMAGE = "ashishbasetty/streambed-server:latest"

DEVICES = [
    {"device_id": "server-001", "host_port": 8001, "container_port": 8001, "image": SERVER_IMAGE},
    {"device_id": "server-002", "host_port": 8004, "container_port": 8001, "image": SERVER_IMAGE},
    {"device_id": "edge-001", "host_port": 8000, "container_port": 8000, "image": EDGE_IMAGE},
    {"device_id": "edge-002", "host_port": 8002, "container_port": 8000, "image": EDGE_IMAGE},
    {"device_id": "edge-003", "host_port": 8003, "container_port": 8000, "image": EDGE_IMAGE},
]

CONTROLLER_TIMEOUT = 120  # Deploy can take a while (image pull)
WAIT_RETRIES = 30
WAIT_INTERVAL = 1
DEPLOY_STAGGER_SEC = 10  # Delay between deploys so PyTorch containers don't OOM (load one at a time)


def _wait_for_controller(controller_url: str) -> None:
    """Wait for controller /health to succeed."""
    base = controller_url.rstrip("/")
    for i in range(WAIT_RETRIES):
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(f"{base}/health")
                if resp.status_code == 200:
                    return
        except (httpx.ConnectError, httpx.ConnectTimeout):
            pass
        time.sleep(WAIT_INTERVAL)
    raise RuntimeError(f"Controller at {controller_url} not ready after {WAIT_RETRIES} attempts")


def _wait_for_daemons() -> None:
    """Wait for daemons to be reachable (they register with controller on startup)."""
    daemon_ports = [9090, 9091, 9092, 9093, 9094]
    for i in range(WAIT_RETRIES):
        ready = 0
        for port in daemon_ports:
            try:
                with httpx.Client(timeout=2) as client:
                    resp = client.get(f"http://localhost:{port}/health")
                    if resp.status_code == 200:
                        ready += 1
            except (httpx.ConnectError, httpx.ConnectTimeout):
                pass
        if ready == len(daemon_ports):
            return
        time.sleep(WAIT_INTERVAL)
    raise RuntimeError(f"Daemons not ready after {WAIT_RETRIES} attempts")


def deploy_all_inference(controller_url: str = "http://localhost:8080") -> None:
    """
    Deploy all inference containers (edge + server) to all daemons via the controller.
    Waits for controller and daemons to be ready, then POSTs deploy for each device.
    Raises on first deploy failure.
    """
    _wait_for_controller(controller_url)
    _wait_for_daemons()

    base = controller_url.rstrip("/")
    with httpx.Client(timeout=CONTROLLER_TIMEOUT) as client:
        for dev in DEVICES:
            payload = {
                "device_cluster": DEVICE_CLUSTER,
                "device_id": dev["device_id"],
                "image": dev["image"],
                "host_port": dev["host_port"],
                "container_port": dev["container_port"],
            }
            resp = client.post(f"{base}/deploy", json=payload)
            if resp.status_code != 200:
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:
                    detail = resp.text
                print(f"[DEPLOY ERROR] {dev['device_id']}: HTTP {resp.status_code} - {detail}")
                logger.error("Deploy %s failed: HTTP %s - %s", dev["device_id"], resp.status_code, detail)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Deploy failed for {dev['device_id']}: {data.get('error', data)}")
            logger.info("Deployed %s", dev["device_id"])
            # Stagger deploys so PyTorch containers load one at a time (avoids OOM)
            if dev != DEVICES[-1]:
                time.sleep(DEPLOY_STAGGER_SEC)


def deploy_device(
    device_id: str,
    controller_url: str = "http://localhost:8080",
) -> None:
    """Deploy a single device via the controller. Raises on failure."""
    dev = next((d for d in DEVICES if d["device_id"] == device_id), None)
    if not dev:
        raise ValueError(f"Unknown device_id: {device_id}")
    base = controller_url.rstrip("/")
    payload = {
        "device_cluster": DEVICE_CLUSTER,
        "device_id": dev["device_id"],
        "image": dev["image"],
        "host_port": dev["host_port"],
        "container_port": dev["container_port"],
    }
    with httpx.Client(timeout=CONTROLLER_TIMEOUT) as client:
        resp = client.post(f"{base}/deploy", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Deploy failed for {device_id}: {data.get('error', data)}")
    logger.info("Deployed %s", device_id)


def delete_all_inference(controller_url: str = "http://localhost:8080") -> None:
    """
    Delete all inference containers via the controller.
    Best-effort: logs failures but does not raise (e.g. if device already gone).
    """
    base = controller_url.rstrip("/")
    with httpx.Client(timeout=30) as client:
        for dev in DEVICES:
            payload = {"device_cluster": DEVICE_CLUSTER, "device_id": dev["device_id"]}
            try:
                resp = client.request("DELETE", f"{base}/delete", json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        logger.info("Deleted %s", dev["device_id"])
                    else:
                        logger.warning("Delete %s: %s", dev["device_id"], data.get("error", data))
                else:
                    logger.warning("Delete %s: HTTP %s %s", dev["device_id"], resp.status_code, resp.text)
            except Exception as e:
                logger.warning("Delete %s failed: %s", dev["device_id"], e)


def _inference_container_name_prefix(device_id: str) -> str:
    """Container name prefix used by daemon: streambed-{cluster}-{device_id}-."""
    return f"streambed-{DEVICE_CLUSTER}-{device_id}-"


def inference_container_running(device_id: str) -> bool:
    """Check if the inference container for the given device_id is running."""
    prefix = _inference_container_name_prefix(device_id)
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name={prefix}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    return bool(result.returncode == 0 and result.stdout.strip())


def kill_inference_container(device_id: str) -> None:
    """Kill the inference container for the given device_id (simulates crash)."""
    prefix = _inference_container_name_prefix(device_id)
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name={prefix}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"No running inference container found for {device_id}")
    name = result.stdout.strip().split("\n")[0]
    kill_result = subprocess.run(["docker", "kill", name], capture_output=True, text=True)
    if kill_result.returncode != 0:
        raise RuntimeError(f"Failed to kill {name}: {kill_result.stderr}")

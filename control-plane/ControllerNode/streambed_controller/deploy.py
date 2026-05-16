"""Deploy container logic: forwards deploy requests to the deployment daemon."""
import logging
import re
import time

import httpx

from .db import (
    bootstrap_sidecar_endpoint,
    delete_deployment,
    get_connection,
    get_device_address,
    get_last_deployment,
    record_deployment,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_SEC = 2
TIMEOUT_SEC = 120  # Pull/run can take a while

def validate_device_id(device_id: str) -> None:
    """Validate the device ID."""
    if not re.match(r'^[a-zA-Z0-9-]+$', device_id):
        raise ValueError("Invalid device ID")

def deploy_to_device(
    device_cluster: str,
    device_id: str,
    device_type: str,
    image: str,
    host_port: int | None = None,
    container_port: int | None = None,
    controller_url: str | None = None,
    video_server_host: str | None = None,
    video_server_port: int | None = None,
) -> dict:
    """
    Send deploy request to the daemon on the target device.
    Blocks until success or final failure after retries.
    Returns {"ok": True} on success, raises DeployError on failure.
    """
    validate_device_id(device_id)

    addr = get_device_address(device_cluster, device_id)
    if not addr:
        raise DeviceNotFoundError(f"Device {device_cluster}/{device_id} not found in registry")
    ip, port = addr

    url = f"http://{ip}:{port}/deploy"
    payload: dict = {"image": image, "device_type": device_type}
    if host_port is not None:
        payload["host_port"] = host_port
    if container_port is not None:
        payload["container_port"] = container_port
    if controller_url is not None:
        payload["controller_url"] = controller_url
    if video_server_host is not None:
        payload["video_server_host"] = video_server_host
    if video_server_port is not None:
        payload["video_server_port"] = video_server_port

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with httpx.Client(timeout=TIMEOUT_SEC) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if data.get("ok"):
                    sidecar_host_ip = data.get("sidecar_host_ip")
                    sidecar_host_port = data.get("sidecar_host_port")
                    if not sidecar_host_ip or not isinstance(sidecar_host_port, int):
                        raise DeployError(
                            "Daemon /deploy response missing sidecar_host_ip/sidecar_host_port"
                        )
                    try:
                        record_deployment(
                            device_cluster,
                            device_id,
                            device_type,
                            image,
                            host_port=host_port,
                            container_port=container_port,
                            container_hash=data.get("container_hash"),
                            container_name=data.get("container_name"),
                            status="running",
                        )
                        # Bootstrap the sidecar endpoint into device_status so
                        # routing pushes work before the sidecar's first
                        # heartbeat arrives. The sidecar heartbeat is
                        # authoritative thereafter.
                        bootstrap_sidecar_endpoint(
                            device_cluster, device_id, sidecar_host_ip, sidecar_host_port
                        )
                    except Exception as e:
                        raise DeployError(f"Deploy succeeded but failed to record: {e}") from e
                    if device_type == "server":
                        # Push the new sidecar endpoint to every edge currently
                        # routed at this server so streaming can resume without
                        # waiting on the periodic sync tick.
                        _push_target_to_routed_edges(
                            device_cluster, device_id, sidecar_host_ip, sidecar_host_port
                        )
                    return data
                raise DeployError(data.get("error", "Daemon returned failure"))
        except httpx.HTTPStatusError as e:
            last_error = DeployError(f"Daemon returned {e.response.status_code}: {e.response.text}")
        except httpx.RequestError as e:
            last_error = DeployError(f"Request failed: {e}")
        except DeployError:
            raise

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY_SEC)

    raise last_error or DeployError("Deployment failed")

def delete_container_from_device(
    device_cluster: str,
    device_id: str,
    soft_delete: bool = False # If True, do not remove deployment record from database
) -> dict:
    """
    Send delete request to the daemon on the target device.
    Returns {"ok": True} on success, raises DeployError on failure.
    """
    validate_device_id(device_id)

    addr = get_device_address(device_cluster, device_id)
    if not addr:
        raise DeviceNotFoundError(f"Device {device_cluster}/{device_id} not found in registry")
    ip, port = addr
    url = f"http://{ip}:{port}/delete"
    deployment = get_last_deployment(device_cluster, device_id)
    payload: dict = {}
    if deployment and deployment.get("container_name"):
        payload["container_name"] = deployment["container_name"]
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with httpx.Client(timeout=TIMEOUT_SEC) as client:
                resp = client.request("DELETE", url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if data.get("ok"):
                    if not soft_delete:
                        try:
                            delete_deployment(device_cluster, device_id)
                        except Exception as e:
                            raise DeployError(f"Delete succeeded but failed to record: {e}") from e
                    return data
                raise DeployError(data.get("error", "Daemon returned failure"))
        except httpx.HTTPStatusError as e:
            last_error = DeployError(f"Daemon returned {e.response.status_code}: {e.response.text}")
        except httpx.RequestError as e:
            last_error = DeployError(f"Request failed: {e}")
        except DeployError:
            raise

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY_SEC)

    raise last_error or DeployError("Delete failed")


def _push_target_to_routed_edges(
    cluster: str,
    target_server: str,
    target_ip: str,
    target_port: int,
) -> None:
    """Synchronously PUT /stream-target on every edge routed to target_server.

    Best-effort: log and continue on per-edge failure. The periodic
    `_sync_stream_targets_from_routing` tick is the durable fallback.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT source_device FROM routing WHERE source_cluster=? AND target_device=?",
            (cluster, target_server),
        ).fetchall()
    finally:
        conn.close()
    edge_ids = [r[0] for r in rows]
    if not edge_ids:
        return
    with httpx.Client(timeout=5.0) as client:
        for edge_id in edge_ids:
            addr = get_device_address(cluster, edge_id)
            if not addr:
                logger.warning(f"{cluster}/{edge_id}: address unknown; skipping target push")
                continue
            edge_ip, daemon_port = addr
            url = f"http://{edge_ip}:{daemon_port}/stream-target"
            try:
                resp = client.put(url, json={"target_ip": target_ip, "target_port": target_port})
                resp.raise_for_status()
                logger.info(
                    f"{cluster}/{edge_id}: target pushed -> {target_ip}:{target_port}"
                )
            except httpx.HTTPError as e:
                logger.warning(f"{cluster}/{edge_id}: target push failed: {e}")


class DeployError(Exception):
    """Raised when deployment fails after retries."""


class DeviceNotFoundError(DeployError):
    """Raised when the target device is not in the registry."""

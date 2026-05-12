"""Deploy container logic: forwards deploy requests to the deployment daemon."""
import re
import time

import httpx
from db import delete_deployment, get_device_address, get_last_deployment, record_deployment

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
                    try:
                        record_deployment(
                            device_cluster,
                            device_id,
                            device_type,
                            image,
                            host_port,
                            container_port,
                            managing_daemon_id=device_id,
                            container_hash=data.get("container_hash"),
                            container_name=data.get("container_name"),
                            sidecar_name=data.get("sidecar_name"),
                            status="running",
                        )
                    except Exception as e:
                        raise DeployError(f"Deploy succeeded but failed to record: {e}") from e
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
    if deployment:
        if deployment.get("container_name"):
            payload["container_name"] = deployment["container_name"]
        if deployment.get("sidecar_name"):
            payload["sidecar_name"] = deployment["sidecar_name"]
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


class DeployError(Exception):
    """Raised when deployment fails after retries."""


class DeviceNotFoundError(DeployError):
    """Raised when the target device is not in the registry."""

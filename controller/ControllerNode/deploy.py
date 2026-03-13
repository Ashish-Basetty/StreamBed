"""Deploy container logic: forwards deploy requests to the deployment daemon."""
import time
import os
import httpx
import re

from db import get_device_address, record_deployment

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
    image: str,
    host_port: int | None = None,
    container_port: int | None = None,
    controller_url: str | None = None,
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
    payload: dict = {"image": image}
    if host_port is not None:
        payload["host_port"] = host_port
    if container_port is not None:
        payload["container_port"] = container_port
    if controller_url is not None:
        payload["controller_url"] = controller_url

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with httpx.Client(timeout=TIMEOUT_SEC) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if data.get("ok"):
                    # Record successful deployment
                    record_deployment(device_cluster, device_id, image, host_port, container_port)
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
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with httpx.Client(timeout=TIMEOUT_SEC) as client:
                resp = client.delete(url)
                resp.raise_for_status()
                data = resp.json()
                if data.get("ok"):
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

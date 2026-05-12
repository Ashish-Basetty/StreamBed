"""Reusable controller heartbeat loop for inference containers."""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from shared.interfaces.heartbeat_spec import HeartbeatStatus

log = logging.getLogger("streambed_heartbeat")

DEFAULT_MODEL_VERSION = "unknown"


async def heartbeat_loop(*, model_version: str = DEFAULT_MODEL_VERSION) -> None:
    """Send controller heartbeats using the deployment daemon's standard env vars.

    Inference containers launched by the daemon receive `CONTROLLER_URL`,
    `DEVICE_CLUSTER`, and `DEVICE_ID`. Containers outside the daemon can call
    this too; if the required env vars are absent, the loop exits quietly.
    """
    controller_url = os.environ.get("CONTROLLER_URL", "").strip()
    device_cluster = os.environ.get("DEVICE_CLUSTER", "default")
    device_id = os.environ.get("DEVICE_ID", "").strip()
    interval = float(os.environ.get("HEARTBEAT_INTERVAL", "20"))

    if not controller_url or not device_id:
        log.info("heartbeat disabled; CONTROLLER_URL or DEVICE_ID is unset")
        return

    url = f"{controller_url.rstrip('/')}/heartbeat"
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    url,
                    json={
                        "device_cluster": device_cluster,
                        "device_id": device_id,
                        "current_model_version": model_version,
                        "status": HeartbeatStatus.ACTIVE.value,
                    },
                )
        except Exception as e:
            log.warning("heartbeat failed: %s", e)
        await asyncio.sleep(interval)

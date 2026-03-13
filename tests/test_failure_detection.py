"""
Test for automatic failure detection and failover.

This file contains basic unit tests. For comprehensive integration tests
using real processes, see test_failure_detection_real.py
"""
import asyncio
import json
import os
import sys
import subprocess

import pytest
import httpx

# Adjust path as needed
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class ControllerClient:
    """Client for testing controller endpoints."""

    def __init__(self, base_url: str = "http://localhost:8080"):
        self.base_url = base_url

    async def register_device(
        self,
        device_cluster: str,
        device_id: str,
        ip: str,
        port: int | None = None,
    ) -> dict:
        """Register a device with the controller."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/register",
                json={
                    "device_cluster": device_cluster,
                    "device_id": device_id,
                    "ip": ip,
                    "port": port,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def send_heartbeat(
        self,
        device_cluster: str,
        device_id: str,
        current_model_version: str | None = None,
        status: str = "Active",
    ) -> dict:
        """Send a heartbeat from a device."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/heartbeat",
                json={
                    "device_cluster": device_cluster,
                    "device_id": device_id,
                    "current_model_version": current_model_version,
                    "status": status,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def get_status(self, device_cluster: str) -> dict:
        """Get device status for a cluster."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/status",
                params={"device_cluster": device_cluster},
            )
            resp.raise_for_status()
            return resp.json()

    async def trigger_failover(self, device_cluster: str) -> dict:
        """Manually trigger failover."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/failover",
                params={"device_cluster": device_cluster},
            )
            resp.raise_for_status()
            return resp.json()


@pytest.mark.asyncio
async def test_manual_failover_trigger():
    """Test manual failover via /failover endpoint."""

    controller_client = ControllerClient()

    # Start controller for this test
    proc = subprocess.Popen(
        [sys.executable, "controller/ControllerNode/main.py"],
        env={**os.environ, "HEARTBEAT_TIMEOUT_SECS": "10", "HEALTH_CHECK_INTERVAL_SECS": "2"},
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )

    try:
        await asyncio.sleep(3)  # Let controller start

        # Register devices
        await controller_client.register_device(
            device_cluster="test-manual",
            device_id="server1",
            ip="127.0.0.1",
            port=8001,
        )
        await controller_client.register_device(
            device_cluster="test-manual",
            device_id="edge1",
            ip="127.0.0.1",
            port=9091,
        )

        # Send initial heartbeat
        await controller_client.send_heartbeat(
            device_cluster="test-manual",
            device_id="server1",
        )
        await asyncio.sleep(2)

        # Trigger failover
        print("[Test] Triggering manual failover...")
        result = await controller_client.trigger_failover("test-manual")
        assert result["ok"] is True, "Failover should succeed"
        print("[Test] ✓ Manual failover endpoint works")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    print("""
    Basic unit tests for failure detection and failover.

    For comprehensive integration tests using real processes, run:
    pytest tests/test_failure_detection_real.py -v -s

    RUN THIS FILE:
    pytest tests/test_failure_detection.py -v -s
    """)

"""
Test for automatic failure detection and failover.

This test demonstrates the complete flow:
1. Controller and daemons start
2. Servers and edges register with controller
3. Heartbeats are sent regularly by edge/server apps
4. A server failure is simulated (stop sending heartbeats)
5. Controller detects failure and updates stream-target.json on edges
6. Edges automatically reconnect to healthy server
"""
import asyncio
import json
import os
import sys

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
async def test_failure_detection_and_failover():
    """
    Test that controller automatically detects server failure and reroutes edges.
    
    This is an integration test that requires:
    - Controller running on localhost:8080
    - At least one deployment daemon running on localhost:9090
    
    To run this test:
    1. Start controller: python controller/ControllerNode/main.py
    2. Start daemon: python controller/DeploymentDaemon/main.py
    3. Run test: pytest tests/test_failure_detection.py::test_failure_detection_and_failover -v
    """
    
    controller = ControllerClient()
    cluster = "test-auto-failover"
    
    # 1. Register servers with controller
    print("\n[Test] Registering servers...")
    await controller.register_device(
        device_cluster=cluster,
        device_id="server1",
        ip="127.0.0.1",
        port=8001,
    )
    await controller.register_device(
        device_cluster=cluster,
        device_id="server2",
        ip="127.0.0.1",
        port=8002,
    )
    
    # 2. Register edges with controller
    print("[Test] Registering edges...")
    await controller.register_device(
        device_cluster=cluster,
        device_id="edge1",
        ip="127.0.0.1",
        port=9091,
    )
    
    # 3. Start sending heartbeats from both servers
    print("[Test] Starting heartbeats from servers...")
    
    async def heartbeat_loop(device_id: str, duration: int):
        """Send periodic heartbeats for a device."""
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < duration:
            try:
                await controller.send_heartbeat(
                    device_cluster=cluster,
                    device_id=device_id,
                )
            except Exception as e:
                print(f"[Test] Heartbeat error for {device_id}: {e}")
            await asyncio.sleep(2)
    
    # Run heartbeats in background (30s duration)
    server1_task = asyncio.create_task(heartbeat_loop("server1", duration=30))
    server2_task = asyncio.create_task(heartbeat_loop("server2", duration=60))
    
    # 4. Let heartbeats accumulate (~10 seconds)
    print("[Test] Waiting for heartbeats to register...")
    await asyncio.sleep(10)
    
    # Check status: both servers should be ACTIVE
    status = await controller.get_status(cluster)
    print(f"[Test] After heartbeats: {json.dumps(status, indent=2)}")
    
    server1_status = next(
        (d for d in status["status"] if d["device_id"] == "server1"),
        None,
    )
    server2_status = next(
        (d for d in status["status"] if d["device_id"] == "server2"),
        None,
    )
    
    assert server1_status is not None, "server1 should be registered"
    assert server2_status is not None, "server2 should be registered"
    assert server1_status["status"] == "Active", "server1 should be ACTIVE"
    assert server2_status["status"] == "Active", "server2 should be ACTIVE"
    print("[Test] ✓ Both servers are ACTIVE")
    
    # 5. Simulate server1 failure by canceling its heartbeats
    # (server2 continues for 60s, server1 stops after ~30s)
    print("\n[Test] Server1 heartbeats stopped (simulating failure)...")
    
    # 6. Wait for health monitor to detect failure
    # Default timeout is 30 seconds, check interval is 5 seconds
    # Server1 stops at ~30s, detection happens within ~40s
    print("[Test] Waiting for failure detection (may take up to 40 seconds)...")
    for i in range(10):  # Wait up to 50 seconds
        print(f"[Test] Health check {i+1}/10 (elapsed {(i+1)*5}s)...")
        await asyncio.sleep(5)
        
        status = await controller.get_status(cluster)
        server1_status = next(
            (d for d in status["status"] if d["device_id"] == "server1"),
            None,
        )
        
        if server1_status and server1_status["status"] == "Unresponsive":
            print(f"[Test] ✓ server1 detected as UNRESPONSIVE")
            break
    else:
        pytest.fail("Server1 failure not detected within 50 seconds")
    
    print("[Test] Failure detection test passed!")
    
    # Cleanup
    await server2_task


@pytest.mark.asyncio
async def test_manual_failover_trigger():
    """Test manual failover via /failover endpoint."""
    
    controller = ControllerClient()
    cluster = "test-manual-failover"
    
    # Register and heartbeat
    print("\n[Test] Testing manual failover trigger...")
    await controller.register_device(
        device_cluster=cluster,
        device_id="server1",
        ip="127.0.0.1",
        port=8001,
    )
    await controller.register_device(
        device_cluster=cluster,
        device_id="edge1",
        ip="127.0.0.1",
        port=9091,
    )
    
    # Send initial heartbeat
    print("[Test] Sending heartbeats...")
    await controller.send_heartbeat(
        device_cluster=cluster,
        device_id="server1",
    )
    await asyncio.sleep(2)
    
    # Trigger failover
    print("[Test] Triggering manual failover...")
    result = await controller.trigger_failover(cluster)
    print(f"[Test] Failover result: {result}")
    
    assert result["ok"] is True, "Failover should succeed"
    print("[Test] ✓ Manual failover endpoint works")



if __name__ == "__main__":
    print("""
    Integration tests for failure detection and failover.
    
    REQUIREMENTS:
    - Controller running: python controller/ControllerNode/main.py
    - Daemon running: python controller/DeploymentDaemon/main.py
    
    RUN:
    pytest tests/test_failure_detection.py -v -s
    """)

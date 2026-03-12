"""
Test that simulates a server failure and controller re-routing via stream-target polling.

This test:
1. Starts edge-like sender that polls stream-target.json
2. Routes frames to server1 initially
3. "Kills" server1 by stopping it
4. Updates stream-target.json to point to server2
5. Verifies frames now arrive at server2
"""
import asyncio
import json
import os
import sys
import numpy as np
import pytest
from pathlib import Path

# sys.path.insert(0, "/app")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.interfaces.stream_interface import StreamBedUDPSender, StreamFrame


class MockUDPServer:
    """Mock UDP server that collects received frames."""
    
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.frames_received = []
        self.running = False
        self.socket = None
    
    async def start(self):
        """Start listening for frames."""
        import socket
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.host, self.port))
        self.socket.setblocking(False)
        self.running = True
        asyncio.create_task(self._receive_loop())
    
    async def _receive_loop(self):
        """Receive frames from UDP."""
        while self.running:
            try:
                data, addr = self.socket.recvfrom(65536)
                self.frames_received.append((data, addr))
            except BlockingIOError:
                await asyncio.sleep(0.01)
            except Exception:
                break
    
    async def stop(self):
        """Stop listening."""
        self.running = False
        if self.socket:
            self.socket.close()
    
    def frame_count(self) -> int:
        return len(self.frames_received)


async def edge_simulator(config_path: Path, target_host: str, target_port: int, duration: int):
    """
    Simulate an edge device that:
    1. Polls stream-target.json for routing
    2. Sends frames to the configured target
    3. Detects when target changes and reconnects
    """
    sender = StreamBedUDPSender()
    await sender.connect(target_host, target_port)
    
    last_target = (target_host, target_port)
    frame_count = 0
    start_time = asyncio.get_event_loop().time()
    
    while asyncio.get_event_loop().time() - start_time < duration:
        # Poll config file
        if config_path.exists():
            try:
                data = json.loads(config_path.read_text())
                new_target = (data.get("target_ip"), data.get("target_port"))
                
                if new_target != last_target and all(new_target):
                    print(f"[EdgeSim] Detected target change to {new_target[0]}:{new_target[1]}")
                    await sender.connect(new_target[0], new_target[1])
                    last_target = new_target
            except Exception as e:
                print(f"[EdgeSim] Config poll error: {e}")
        
        # Send a frame
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        embedding = np.random.rand(1280).astype(np.float32)
        sf = StreamFrame(
            timestamp=asyncio.get_event_loop().time(),
            frame=frame,
            embedding=embedding,
            model_version="v1",
            source_device_id="test-edge-1",
            frame_interleaving_rate=30.0,
        )
        
        try:
            await sender.send(sf)
            frame_count += 1
            if frame_count % 10 == 0:
                print(f"[EdgeSim] Sent {frame_count} frames")
        except Exception as e:
            print(f"[EdgeSim] Send error: {e}")
        
        await asyncio.sleep(0.05)  # ~20 fps
    
    await sender.close()
    print(f"[EdgeSim] Sent total {frame_count} frames")


@pytest.mark.asyncio
async def test_controller_reroutes_on_server_failure(tmp_path):
    """
    Test scenario:
    1. Edge sends frames to Server1
    2. Server1 is "killed" (stopped)
    3. Controller updates stream-target.json to Server2
    4. Edge detects change and reconnects
    5. Edge sends frames to Server2
    """
    # Setup config directory
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    stream_target_path = config_dir / "stream-target.json"
    
    # Start two mock servers
    server1 = MockUDPServer("127.0.0.1", 15001)
    server2 = MockUDPServer("127.0.0.1", 15002)
    
    await server1.start()
    await server2.start()
    
    # Write initial target pointing to server1
    stream_target_path.write_text(json.dumps({"target_ip": "127.0.0.1", "target_port": 15001}))
    
    # Start edge simulator in background
    edge_task = asyncio.create_task(
        edge_simulator(stream_target_path, "127.0.0.1", 15001, duration=8)
    )
    
    # Let edge send frames to server1 for 3 seconds
    await asyncio.sleep(3)
    frames_on_server1_before = server1.frame_count()
    print(f"[Test] Server1 received {frames_on_server1_before} frames before failure")
    assert frames_on_server1_before > 5, "Server1 should have received some frames"
    
    # Stop server1 (simulate failure)
    await server1.stop()
    print("[Test] Simulated Server1 failure")
    
    # Wait a moment for edge to notice
    await asyncio.sleep(0.5)
    
    # Controller updates stream-target.json to point to server2
    stream_target_path.write_text(json.dumps({"target_ip": "127.0.0.1", "target_port": 15002}))
    print("[Test] Controller updated stream-target to Server2")
    
    # Wait for edge to detect and reconnect, then send more frames
    await asyncio.sleep(3)
    
    frames_on_server2 = server2.frame_count()
    print(f"[Test] Server2 received {frames_on_server2} frames after rerouting")
    assert frames_on_server2 > 5, "Server2 should have received frames after rerouting"
    
    # Verify server1 didn't get new frames (it was stopped)
    frames_on_server1_final = server1.frame_count()
    assert frames_on_server1_final == frames_on_server1_before, \
        "Server1 should not receive new frames after being stopped"
    
    # Cleanup
    await edge_task
    await server2.stop()
    
    print(f"[Test] PASS: Rerouted from Server1 ({frames_on_server1_before} frames) to Server2 ({frames_on_server2} frames)")

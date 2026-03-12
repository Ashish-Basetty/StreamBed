# StreamBed Failure Detection & Failover System

## Overview

The controller now includes automatic failure detection and failover capabilities. It:

1. **Monitors heartbeats** from all servers and edge devices
2. **Detects failures** when devices stop sending heartbeats
3. **Automatically reroutes** edge devices to healthy servers by updating `stream-target.json`

## Architecture

```
Edge Device (e.g., edge1)
    └─ Polls stream-target.json every ~5 seconds
    └─ Sends frames to configured server

Deployment Daemon (port 9090)
    └─ Exposes /stream-target endpoints for reading/writing config
    └─ Manages shared volume where stream-target.json lives

Controller Node (port 8080)
    ├─ /heartbeat endpoint: receives heartbeat from devices
    ├─ /status endpoint: queries device health status
    ├─ /failover endpoint: manually triggers failover
    └─ Health Monitor (async background task):
        ├─ Polls device status every ~5 seconds
        ├─ Detects unresponsive devices (no heartbeat > 30 seconds)
        └─ Automatically calls /stream-target on edge daemons to reroute
```

## Configuration

The health monitor is controlled by environment variables on the controller:

```bash
# Seconds since last heartbeat before marking device as unresponsive (default: 30)
HEARTBEAT_TIMEOUT_SECS=30

# Interval between health checks (default: 5)
HEALTH_CHECK_INTERVAL_SECS=5
```

## How It Works: Automatic Failover Flow

### 1. Normal Operation

```
Controller Health Monitor (every 5 seconds):
  ├─ Check all devices in cluster
  ├─ server1: last heartbeat 2s ago → ACTIVE ✓
  ├─ server2: last heartbeat 1s ago → ACTIVE ✓
  └─ edge1:   last heartbeat 3s ago → ACTIVE ✓
```

### 2. Server Failure Detected

```
Controller Health Monitor:
  ├─ server1: last heartbeat 45s ago → UNRESPONSIVE ✗
  └─ Failover triggered: Find healthy servers
      ├─ Healthy servers: [server2]
      ├─ Reroute edge1 to server2
      └─ Call HTTP PUT to edge1's daemon:
          POST http://edge1_ip:9090/stream-target
          {
              "target_ip": "server2_ip",
              "target_port": 8080
          }
```

### 3. Edge Detects Change

```
Edge Device (polls every ~5 seconds):
  └─ Reads /config/stream-target.json
  └─ Sees target changed to server2
  └─ Reconnects StreamBedUDPSender to server2
  └─ Continues sending frames to server2
```

## API Usage Examples

### 1. Send Heartbeat (from your device)

```bash
curl -X POST http://controller:8080/heartbeat \
  -H "Content-Type: application/json" \
  -d '{
    "device_cluster": "production",
    "device_id": "server1",
    "current_model_version": "v1",
    "status": "Active"
  }'
```

### 2. Check Cluster Status

```bash
curl http://controller:8080/status?device_cluster=production
```

Response:

```json
{
  "status": [
    {
      "device_cluster": "production",
      "device_id": "server1",
      "current_model": "v1",
      "status": "Unresponsive",
      "last_heartbeat": "2025-03-12 10:15:30.123456"
    },
    {
      "device_cluster": "production",
      "device_id": "server2",
      "current_model": "v1",
      "status": "Active",
      "last_heartbeat": "2025-03-12 10:15:45.654321"
    }
  ]
}
```

### 3. Manually Trigger Failover (useful for testing)

```bash
curl -X POST http://controller:8080/failover?device_cluster=production
```

## Integration with Your Test

Your existing test case can now verify automatic failover:

```python
@pytest.mark.asyncio
async def test_automatic_failover():
    """Test that controller automatically reroutes on server failure."""

    # 1. Start mock servers
    server1 = MockUDPServer("127.0.0.1", 15001)
    server2 = MockUDPServer("127.0.0.1", 15002)
    await server1.start()
    await server2.start()

    # 2. Register with controller
    async with httpx.AsyncClient() as client:
        await client.post(
            "http://controller:8080/register",
            json={
                "device_cluster": "test-cluster",
                "device_id": "server1",
                "ip": "127.0.0.1",
                "port": 15001
            }
        )
        await client.post(
            "http://controller:8080/register",
            json={
                "device_cluster": "test-cluster",
                "device_id": "server2",
                "ip": "127.0.0.1",
                "port": 15002
            }
        )

    # 3. Send initial heartbeats
    async with httpx.AsyncClient() as client:
        await client.post(
            "http://controller:8080/heartbeat",
            json={
                "device_cluster": "test-cluster",
                "device_id": "server1",
                "status": "Active"
            }
        )
        await client.post(
            "http://controller:8080/heartbeat",
            json={
                "device_cluster": "test-cluster",
                "device_id": "server2",
                "status": "Active"
            }
        )

    # 4. Stop server1 (simulate failure)
    await server1.stop()

    # 5. Wait for health monitor to detect failure (~30 seconds)
    # Or trigger manually:
    async with httpx.AsyncClient() as client:
        await client.post(
            "http://controller:8080/failover?device_cluster=test-cluster"
        )

    # 6. Verify server2 gets the traffic
    await asyncio.sleep(1)
    assert server2.frame_count() > 0
```

## Device-Side Implementation

### Edge/Server Heartbeat Sender

Add this to your edge/server apps to send periodic heartbeats:

```python
import asyncio
import httpx
import os

CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://localhost:8080")
DEVICE_CLUSTER = os.environ.get("DEVICE_CLUSTER")
DEVICE_ID = os.environ.get("DEVICE_ID")

async def send_heartbeat():
    """Send periodic heartbeat to controller."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{CONTROLLER_URL}/heartbeat",
                    json={
                        "device_cluster": DEVICE_CLUSTER,
                        "device_id": DEVICE_ID,
                        "status": "Active"
                    }
                )
        except Exception as e:
            print(f"Heartbeat failed: {e}")

        await asyncio.sleep(5)  # Send heartbeat every 5 seconds

# Start in background
asyncio.create_task(send_heartbeat())
```

### Stream-Target Polling (Edge)

Your edge device should continue polling stream-target.json:

```python
async def poll_stream_target(config_path: Path, sender: StreamBedUDPSender):
    """Poll for stream-target changes and reconnect."""
    last_target = None

    while True:
        try:
            if config_path.exists():
                data = json.loads(config_path.read_text())
                new_target = (data.get("target_ip"), data.get("target_port"))

                if new_target != last_target and all(new_target):
                    print(f"Rerouting to {new_target[0]}:{new_target[1]}")
                    await sender.connect(new_target[0], new_target[1])
                    last_target = new_target
        except Exception as e:
            print(f"Stream-target poll error: {e}")

        await asyncio.sleep(5)  # Poll every 5 seconds
```

## Testing the System

1. **Unit Test**: Verify health monitor detects failures

   ```bash
   cd /Users/nutz/VSCode/StreamBed
   pytest tests/test_controller_rerouting.py -v
   ```

2. **Integration Test**: Full end-to-end with real heartbeats

   ```bash
   # Terminal 1: Start controller
   python controller/ControllerNode/main.py

   # Terminal 2: Start deployment daemon (edge/server)
   python controller/DeploymentDaemon/main.py

   # Terminal 3: Register device and send heartbeats
   curl -X POST http://localhost:8080/register \
     -H "Content-Type: application/json" \
     -d '{"device_cluster":"prod","device_id":"server1","ip":"127.0.0.1","port":9090}'

   # Send heartbeats periodically
   for i in {1..100}; do
     curl -X POST http://localhost:8080/heartbeat \
       -H "Content-Type: application/json" \
       -d '{"device_cluster":"prod","device_id":"server1","status":"Active"}'
     sleep 5
   done
   ```

3. **Failure Simulation**: Stop heartbeats and watch rerouting
   ```bash
   # Controller automatically detects failure after 30 seconds
   # And reroutes all edges to healthy servers
   ```

## Database Schema

The controller maintains these tables:

```sql
-- Device registry
CREATE TABLE devices (
    device_cluster TEXT NOT NULL,
    device_id TEXT NOT NULL,
    ip TEXT NOT NULL,
    port INTEGER,
    registered_at TIMESTAMP,
    PRIMARY KEY (device_cluster, device_id)
);

-- Health status
CREATE TABLE device_status (
    device_cluster TEXT NOT NULL,
    device_id TEXT NOT NULL,
    current_model TEXT,
    status TEXT,  -- 'Active', 'Unresponsive', 'Deployment Failure'
    last_heartbeat TIMESTAMP,
    PRIMARY KEY (device_cluster, device_id)
);

-- Routing table (for future use)
CREATE TABLE routing (
    source_cluster TEXT NOT NULL,
    source_device TEXT NOT NULL,
    target_cluster TEXT NOT NULL,
    target_device TEXT NOT NULL,
    updated_at TIMESTAMP,
    PRIMARY KEY (source_cluster, source_device)
);
```

## Troubleshooting

### Edges not rerouting

- Check that the deployment daemon is running on the edge: `curl http://edge_ip:9090/health`
- Verify stream-target.json exists: `cat /config/stream-target.json`
- Check controller logs: `HEARTBEAT_TIMEOUT_SECS=10` (lower timeout for faster detection)

### Device stays UNRESPONSIVE

- Heartbeat endpoint may not be reachable: verify network connectivity
- Check that `CONTROLLER_URL` is correctly set on the device
- Verify device is sending heartbeats: `curl http://controller:8080/status?device_cluster=prod`

### Failover not triggering

- Increase verbosity: `python -u main.py` to see logging
- Manually test with: `curl -X POST http://controller:8080/failover?device_cluster=prod`
- Check `HEARTBEAT_TIMEOUT_SECS` is reasonable for your deployment

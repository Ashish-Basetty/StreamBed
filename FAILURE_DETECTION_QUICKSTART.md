# Quick Start: Automatic Failure Detection & Failover

## Overview

Your StreamBed controller can now automatically detect when servers fail and reroute edge devices to healthy servers. It can also attempt to restart failed edge devices. Here's how:

```
Server1 stops sending heartbeats → Controller detects missing heartbeat →
Updates stream-target.json on Edge1 → Edge1 reconnects to Server2

Edge1 stops sending heartbeats → Controller detects failure →
Attempts to restart Edge1's container → Edge1 recovers and resumes streaming
```

## How It Works

The controller continuously monitors device heartbeats:

1. **Devices send heartbeats** - Every 5 seconds (or your interval)
2. **Controller tracks them** - Records `last_heartbeat` timestamp
3. **Failure detection** - If no heartbeat > 30 seconds, marks as UNRESPONSIVE
4. **Auto-failover** - Detects downed servers, updates `stream-target.json` on edges
5. **Edge restart** - Detects downed edges, attempts to redeploy their containers
6. **Edges reconnect** - They already poll `stream-target.json`, so they reconnect automatically

## Setup (Simple!)

### Step 1: Devices Send Heartbeats

Your edge/server apps already send heartbeats via the `/heartbeat` endpoint. Just make sure they're doing it periodically:

```python
# In your edge/server app (already doing this probably)
import httpx

async def send_heartbeat():
    while True:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{CONTROLLER_URL}/heartbeat",
                    json={
                        "device_cluster": os.environ["DEVICE_CLUSTER"],
                        "device_id": os.environ["DEVICE_ID"],
                        "status": "Active",
                    }
                )
        except:
            pass
        await asyncio.sleep(5)  # Every 5 seconds

asyncio.create_task(send_heartbeat())
```

### Step 2: Start the Controller

```bash
cd /Users/nutz/VSCode/StreamBed

# Optionally tune timeouts
export HEARTBEAT_TIMEOUT_SECS=30        # Seconds before marking unresponsive
export HEALTH_CHECK_INTERVAL_SECS=5     # How often to check

# Start it
python controller/ControllerNode/main.py
```

That's it! The system now automatically detects failures.

## Detection Flow

Controller operation is automatic:

1. Controller checks device status every 5 seconds
2. Server stops sending heartbeats (crashes, network issue, etc)
3. After 30 seconds with no heartbeat, marks as UNRESPONSIVE
4. Detects failures within ~35 seconds total
5. Finds a healthy server and updates `stream-target.json` on edges
6. Edges detect the change and reconnect automatically

## Manual Triggers (For Testing)

```bash
# Check cluster health
curl http://localhost:8080/status?device_cluster=production

# Manually trigger failover check (useful for testing)
curl -X POST http://localhost:8080/failover?device_cluster=production
```

# Manually trigger failover (useful for testing)

curl -X POST http://localhost:8080/failover?device_cluster=production

````

## Verification

### Check Device Status
```bash
curl http://localhost:8080/status?device_cluster=production

# Output:
# {
#   "status": [
#     {
#       "device_cluster": "production",
#       "device_id": "server1",
#       "status": "Active",
#       "last_heartbeat": "2025-03-12 10:15:45.654321"
#     },
#     {
#       "device_id": "edge1",
#       "status": "Active",
#       ...
#     }
#   ]
# }
````

### Simulate Failure & Watch Rerouting

```bash
# Terminal 1: Watch controller logs
python controller/ControllerNode/main.py

# Terminal 2: Kill a server process
kill <server1_pid>

# Watch the logs - within ~35 seconds you'll see:
# [ControllerNode] server1: Marked as UNRESPONSIVE
# [ControllerNode] server1 down, rerouting edges to server2
# [ControllerNode] edge1: Rerouted to 127.0.0.1:8080
```

## Environment Variables

On your **server** and **edge** applications:

```bash
DEVICE_CLUSTER=production      # Cluster name
DEVICE_ID=server1              # Unique device ID
CONTROLLER_URL=http://controller:8080  # Controller address
```

On your **controller**:

```bash
HEARTBEAT_TIMEOUT_SECS=30      # Seconds before marking unresponsive
HEALTH_CHECK_INTERVAL_SECS=5   # How often to check
```

## Testing

### Run the Test Suite

```bash
# Full end-to-end test
pytest tests/test_failure_detection.py -v -s

# Your existing rerouting test (now with auto-failover)
pytest tests/test_controller_rerouting.py -v
```

### Create a Simple Test

```python
import asyncio
import httpx

async def test_auto_failover():
    # Register devices
    async with httpx.AsyncClient() as client:
        await client.post("http://localhost:8080/register", json={
            "device_cluster": "test",
            "device_id": "server1",
            "ip": "127.0.0.1",
            "port": 9090,
        })

    # Stop sending heartbeats (kills the connection)
    # Wait 35+ seconds...

    # Check status
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "http://localhost:8080/status?device_cluster=test"
        )
        status = resp.json()
        # server1 should show "Unresponsive"
        assert status["status"][0]["status"] == "Unresponsive"

asyncio.run(test_auto_failover())
```

## Troubleshooting

| Problem                                    | Solution                                                                |
| ------------------------------------------ | ----------------------------------------------------------------------- |
| Edges not rerouting                        | Check `stream-target.json` is being written to `/config/` on edges      |
| Server stays ACTIVE despite being down     | Decrease `HEARTBEAT_TIMEOUT_SECS` to detect faster                      |
| Failure detection is too slow              | Decrease both `HEARTBEAT_TIMEOUT_SECS` and `HEALTH_CHECK_INTERVAL_SECS` |
| Controller doesn't have /failover endpoint | Update `main.py` with the code from this setup                          |
| Daemon can't reach edge IP                 | Check deployment daemon has correct edge IP registered                  |

## Next Steps

1. **[Optional] Advanced Features**: See [FAILURE_DETECTION_GUIDE.md](FAILURE_DETECTION_GUIDE.md) for advanced configurations
2. **[Optional] Custom Logic**: Modify `health_monitor.py` to implement custom failover strategies
3. **[Optional] Persistent State**: Add database logging of failover events for analytics

## Files Changed

- ✅ `controller/ControllerNode/main.py` - Added health monitor integration
- ✅ `controller/ControllerNode/db.py` - Added status query functions
- ✅ `controller/ControllerNode/health_monitor.py` - NEW: Failure detection logic
- ✅ `shared/edge_stream_manager.py` - NEW: Example edge integration
- ✅ `tests/test_failure_detection.py` - NEW: Integration tests

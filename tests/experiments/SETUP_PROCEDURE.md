# Experiment Setup Procedure

Manual steps to run the time-varying throttle experiment end-to-end.
This is the ground truth for rewriting `test_interleaving_sweep.py`.

## Prerequisites

- Docker Desktop running
- `ashishbasetty/streambed-quic-sidecar:experiment` pushed (multi-arch)
- `ashishbasetty/streambed-advisor-edge:latest` and `ashishbasetty/streambed-advisor-server:latest` pushed
- `tests/experiments/runtime/schedule.yml` contains the stepped_burst schedule
- conda env `streambed` has frame_gen dependencies

## Step 1 — Full teardown

```bash
docker compose -f docker-compose.yml -f docker-compose.varying.yml down
# Force-remove any lingering advisor containers (daemon-spawned, not in compose)
docker ps --format "{{.Names}}" | xargs -r docker stop
docker ps -a --filter "name=streambed" --format "{{.Names}}" | xargs -r docker rm
```

## Step 2 — Bring up base stack + proxy together

**Do NOT bring up the router.** It PUTs `/stream-target` every 30s and will
override the proxy redirect.

```bash
docker compose -f docker-compose.yml -f docker-compose.varying.yml \
  up -d controller daemon-edge1 daemon-server1 varying-proxy
```

Wait for controller:
```bash
until curl -sf http://localhost:8080/status > /dev/null; do sleep 2; done
```

## Step 3 — Deploy advisor containers

```bash
CONTROLLER_URL=http://localhost:8080 \
ADVISOR_EDGE_IMAGE=ashishbasetty/streambed-advisor-edge:latest \
ADVISOR_SERVER_IMAGE=ashishbasetty/streambed-advisor-server:latest \
bash experiments/advisor/scripts/deploy_advisor.sh
```

Note the **server sidecar host port** from the response, e.g. `"sidecar_host_port": 7369`.

## Step 4 — Write proxy target.yml

Point the proxy at the server sidecar's host-exposed UDP port:

```bash
cat > tests/experiments/runtime/target.yml << EOF
target_host: host.docker.internal
target_port: <SERVER_SIDECAR_HOST_PORT>
EOF
```

## Step 5 — Redirect edge through proxy

The deploy script sets the edge's initial stream-target to the server sidecar
directly. We override it after the sidecar is up:

```bash
curl -X PUT http://localhost:9090/stream-target \
  -H "Content-Type: application/json" \
  -d '{"target_ip":"streambed-varying-proxy","target_port":9010}'
```

Wait for the edge to reconnect through the proxy:
```bash
until docker logs streambed-default-edge-001-sidecar 2>&1 \
  | grep "streambed-varying-proxy:9010" | grep -q "QUIC connected"; do sleep 2; done
```

Verify the proxy dialed upstream (will appear after first data packet):
```bash
docker logs streambed-varying-proxy | grep "upstream QUIC connected"
```

## Step 6 — Run frame_gen

The proxy schedule clock starts at proxy container start time. Restart the proxy
just before running frame_gen so t=0 aligns with traffic start:

```bash
docker compose -f docker-compose.yml -f docker-compose.varying.yml \
  up -d --force-recreate varying-proxy
# Re-PUT stream-target immediately after restart
curl -X PUT http://localhost:9090/stream-target \
  -H "Content-Type: application/json" \
  -d '{"target_ip":"streambed-varying-proxy","target_port":9010}'
until docker logs streambed-default-edge-001-sidecar 2>&1 \
  | grep "streambed-varying-proxy:9010" | grep -q "QUIC connected"; do sleep 2; done
```

Then start frame_gen:
```bash
conda run -n streambed python -m experiments.advisor.host.frame_gen \
  --episodes 500 --edge-host 127.0.0.1 --edge-port 9100
```

Run for ~130s (covers all four schedule tiers plus tail).

## Step 7 — Collect CSV

```bash
conda run -n streambed python -c "
import asyncio, time
from pathlib import Path
from datetime import UTC, datetime
from tests.experiments.harness import MetricsPoller, load_schedule

schedule = load_schedule(Path('tests/experiments/schedules/stepped_burst.yml'))
poller = MetricsPoller(
    edge_sidecar='streambed-default-edge-001-sidecar',
    server_sidecar='streambed-default-server-001-sidecar',
    advisor_container='<ADVISOR_CONTAINER>',
    edge_daemon='streambed-daemon-edge1',
    server_daemon='streambed-daemon-server1',
    proxy_schedule=schedule,
)
asyncio.run(poller.start())
time.sleep(130)
asyncio.run(poller.stop())
ts = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
poller.write_csv(Path(f'tests/experiments/results/{ts}__sweep.csv'))
"
```

## Key Gotchas

1. **Router overrides stream-target**: Never start `streambed-router`. It PUTs
   `/stream-target` every 30s and will redirect the edge back to the server
   directly, bypassing the proxy.

2. **Deploy overwrites stream-target**: The daemon stores the deploy payload's
   target. PUT `/stream-target` *after* deploy completes (step 5), not before.

3. **Proxy schedule clock**: Starts at container start, not at frame_gen start.
   Restart the proxy just before starting frame_gen (step 6) to align clocks.

4. **Upstream QUIC connect**: The proxy only dials the server on the first
   datagram from the edge. No data → no upstream connection. Start frame_gen
   to trigger it.

5. **ALPN**: The proxy must negotiate `streambed-quic-v1` on both its inbound
   (edge-facing) and outbound (server-facing) QUIC connections.

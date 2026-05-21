# Experiment Handoff — StreamBed QUIC Relay

## Current State (as of last session)

The full experiment pipeline is nearly working. The QUIC relay proxy terminates
two QUIC connections (edge→proxy and proxy→server) and applies token-bucket
rate limiting on datagrams. Most bugs are fixed. One major blocker remains.

---

## What's Working

1. **UDP forwarder proxy** (`tests/varying_proxy/proxy.py`) — rewritten again
   as a transparent UDP relay. QUIC is **not** terminated; the edge↔server
   QUIC session runs end-to-end and the proxy just shapes the underlying UDP
   packet stream.
   - Inbound: UDP socket on `BIND_PORT` (default 9010)
   - Outbound: per-edge-client UDP socket to `target_host:target_port`
   - Token-bucket rate limiting on edge→server packets; return path unthrottled
   - Loss model + per-packet extra latency from the schedule
   - Schedule (bps/loss_pct/latency) loaded from `tests/experiments/runtime/schedule.yml`
   - Target (server address) loaded from `tests/experiments/runtime/target.yml`
   - Idle flow reaper drops dead clients and forces reconnect when target.yml changes

2. **Sidecar image `ashishbasetty/streambed-quic-sidecar:experiment`** — pushed
   multi-arch (amd64 + arm64) with:
   - `initialBps = 20_000_000` (edge + server) — seeds high so CHNK flows at tier 1
   - `SafetyFactor: 1.0` in SamplingConfig — prevents downward spiral
   - FBCK logging: `edge: FBCK received bps=...`, `server: FBCK recv_bytes=...`
   - 1Hz estimator log: `edge: bw sent_bps=... remote_bps=... composite_bps=...`

3. **Manual setup procedure** — see `tests/experiments/SETUP_PROCEDURE.md`
   for the exact step-by-step (took many sessions to get right).

4. **Harness** (`tests/experiments/harness.py`) — tracks `fbck_bytes_recv` in
   addition to all other metrics. CSV columns include `target_bps`, `throttle_bps`,
   `fbck_bytes_recv`, etc.

---

## Active Blocker: Controller Overriding stream-target

**Root cause**: The **controller** container's health monitor
(`control-plane/ControllerNode/streambed_controller/health_monitor.py`, method
`_sync_stream_targets_from_routing`) runs every 30 seconds and pushes the
routing table to each edge daemon's `/stream-target`. This resets the edge to
`host.docker.internal:7369` (direct to server) every 30s.

Daemon logs show the PUTs coming from `151.101.192.223` — this is **Docker NAT
translation** of the controller container's traffic, not an external IP. (We
initially suspected a GCP VM was still running, but `gcloud compute instances
list` confirmed all GCP VMs are TERMINATED.)

**Evidence**: `docker logs streambed-daemon-edge1 | grep "151.101.192.223"` —
PUTs continue every ~30s even with no process on the host making them, only
stopping when the controller is stopped.

**Fix**: Stop the controller after deploying the advisor:
```bash
docker stop streambed-controller
```
The advisor containers are already running; the controller isn't needed during
the experiment proper. Just don't restart it until you want to redeploy.

---

## Resolved: "control frame too large" Crash

This was a stream-relay corruption bug in the previous aioquic-based proxy:
terminating QUIC on both sides and remapping stream IDs occasionally delivered
stream chunks out of order, which the edge then parsed as a giant control
frame and crashed (`pump exited (control frame too large: 3424760000)`).

**Fix**: stop terminating QUIC at the proxy. The current `proxy.py` is a
plain UDP forwarder, so edge↔server share one end-to-end QUIC session and
there are no stream IDs for the proxy to remap. Rate limiting still works
because dropping UDP packets when the token bucket is empty looks like
network loss to QUIC, and quic-go's congestion controller responds normally.

---

## Reconnect Routing Bug

When the edge's control pump crashes and reconnects, it reads its target from
the daemon. If the daemon has been overwritten to :7369 (by GCP router), the
edge bypasses the proxy on reconnect. Once the GCP router is killed, this
goes away since the daemon will always have :9010.

The daemon persists its stream-target in `daemon-data/edge1/stream-target.json`.
Writing `{"target_ip":"streambed-varying-proxy","target_port":9010}` to this
file AND doing `PUT /stream-target` ensures the daemon's in-memory + on-disk
state both point to the proxy.

---

## Full Setup Recipe (condensed)

```bash
# 1. Teardown
docker compose -f docker-compose.yml -f docker-compose.varying.yml down
docker ps --format "{{.Names}}" | xargs -r docker stop
docker ps -a --filter "name=streambed" --format "{{.Names}}" | xargs -r docker rm

# 2. Bring up (no router)
docker compose -f docker-compose.yml -f docker-compose.varying.yml \
  up -d controller daemon-edge1 daemon-server1 varying-proxy
until curl -sf http://localhost:8080/status > /dev/null; do sleep 2; done

# 3. Deploy advisor (note the server sidecar host port in output)
CONTROLLER_URL=http://localhost:8080 \
ADVISOR_EDGE_IMAGE=ashishbasetty/streambed-advisor-edge:latest \
ADVISOR_SERVER_IMAGE=ashishbasetty/streambed-advisor-server:latest \
bash experiments/advisor/scripts/deploy_advisor.sh

# 4. Write proxy target (use port from step 3)
echo "target_host: host.docker.internal\ntarget_port: PORT" > tests/experiments/runtime/target.yml

# 5. Redirect edge through proxy (after deploy sets edge's initial target)
echo '{"target_ip":"streambed-varying-proxy","target_port":9010}' > daemon-data/edge1/stream-target.json
curl -X PUT http://localhost:9090/stream-target \
  -H "Content-Type: application/json" \
  -d '{"target_ip":"streambed-varying-proxy","target_port":9010}'

# 6. Wait for edge to connect through proxy
until docker logs streambed-default-edge-001-sidecar 2>&1 | \
  grep "streambed-varying-proxy:9010" | grep -q "QUIC connected"; do sleep 2; done

# 7. Restart proxy to reset schedule clock to t=0
docker compose -f docker-compose.yml -f docker-compose.varying.yml \
  up -d --force-recreate varying-proxy
sleep 3
echo '{"target_ip":"streambed-varying-proxy","target_port":9010}' > daemon-data/edge1/stream-target.json
curl -X PUT http://localhost:9090/stream-target \
  -H "Content-Type: application/json" \
  -d '{"target_ip":"streambed-varying-proxy","target_port":9010}'
until docker logs streambed-default-edge-001-sidecar 2>&1 | \
  tail -20 | grep "streambed-varying-proxy:9010" | grep -q "QUIC connected"; do sleep 2; done

# 8. Start frame_gen (conda env: streambed)
conda run -n streambed python -m experiments.advisor.host.frame_gen \
  --episodes 500 --edge-host 127.0.0.1 --edge-port 9100 &
```

---

## Key Files

| File | Purpose |
|------|---------|
| `tests/varying_proxy/proxy.py` | QUIC relay proxy (aioquic) |
| `tests/varying_proxy/Dockerfile` | Proxy container (build context = repo root) |
| `tests/varying_proxy/requirements.txt` | `pyyaml, aioquic>=1.0.0, cryptography>=41.0.0` |
| `tests/experiments/harness.py` | CSV metrics poller |
| `tests/experiments/schedules/stepped_burst.yml` | 20M→2M→500k→100k throttle schedule |
| `tests/experiments/runtime/schedule.yml` | Runtime copy (written by test at startup) |
| `tests/experiments/runtime/target.yml` | Proxy's target host:port |
| `tests/experiments/SETUP_PROCEDURE.md` | Full manual setup steps |
| `docker-compose.varying.yml` | Varying proxy service definition |
| `sidecar/internal/edge/edge.go` | `initialBps=20M`, `SafetyFactor=1.0` |
| `sidecar/internal/server/feedback.go` | `feedbackInitialBps=20M` |

---

## Next Steps

1. **Kill GCP router VM** — stops the 30s stream-target override
2. **Debug stream relay corruption** — use proxy debug logs to find where
   `control frame too large: 3424760000` originates
3. **Run full experiment** — once routing is stable, run for 130s and collect CSV
4. **Rewrite test script** — use `SETUP_PROCEDURE.md` as ground truth;
   test must: (a) NOT start router, (b) write daemon-data file, (c) restart
   proxy just before frame_gen to align schedule clock

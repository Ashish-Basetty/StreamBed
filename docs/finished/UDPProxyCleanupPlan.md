# UDP/TCP Proxy Cleanup Plan

## Context
All five daemons now run with `STREAM_TRANSPORT=quic`. The old "UDP transport" path — a TCP proxy server inside the daemon that edge containers connected to, which then forwarded datagrams to the server peer — is completely dead. `StreamProxyManager`, `tcp_utils.py`, and the three async loop functions in `main.py` are unreachable code. This cleanup removes that entire path and the `STREAM_TRANSPORT` feature flag, leaving QUIC as the only transport.

---

## Files to DELETE entirely

| File | Reason |
|---|---|
| `control-plane/DeploymentDaemon/stream_proxy_manager.py` | UDP/TCP proxy singleton; unused in QUIC mode |
| `control-plane/DeploymentDaemon/tcp_utils.py` | TCP stream handler for the proxy; unused in QUIC mode |
| `tests/test_controller_rerouting.py` | Tests old file-poll rerouting via `stream-target.json`; replaced by `tests/quic/test_dynamic_rerouting.py` |
| `tests/unit/test_tcp_sender.py` | Tests `StreamBedTCPSender`; that class is dead once the TCP proxy is gone |

---

## `shared/interfaces/stream_interface.py`
- Remove `StreamBedTCPSender` class (lines 203–244). `StreamBedUDPSender` / `StreamBedUDPReceiver` stay.

---

## `control-plane/DeploymentDaemon/daemon_config.py`
Remove these constants entirely:
- `STREAM_TRANSPORT`
- `STREAM_PROXY_PORT` — rename to `INGEST_UDP_PORT` (env var `INGEST_UDP_PORT`, default `9000`). This is the port the **server** inference container listens on for UDP frames delivered by the sidecar. The old name implied a proxy; the new name reflects the actual role.
- `STREAM_TARGET_POLL_INTERVAL`
- `BANDWIDTH_POLL_INTERVAL`
- `STREAM_PROXY_HOST`

---

## `control-plane/DeploymentDaemon/main.py`

**Remove imports:**
- `BANDWIDTH_POLL_INTERVAL`, `STREAM_PROXY_HOST`, `STREAM_PROXY_PORT` (→ import `INGEST_UDP_PORT` instead), `STREAM_TARGET_POLL_INTERVAL`, `STREAM_TRANSPORT`
- `StreamProxyManager` from `stream_proxy_manager`
- `_UDPSendOnlyProtocol`, `handle_tcp_stream` from `tcp_utils`
- `SentRateBackend` from `shared.bandwidth`

**Remove functions entirely:**
- `_stream_proxy_target_poll_loop` (lines 113–119)
- `_bandwidth_poll_loop` (lines 122–128)
- `_run_stream_tcp_server` (lines 131–145)

**`_spawn_sidecar_for_role()` — two edits:**
1. Delete the guard: `if STREAM_TRANSPORT != "quic": return None`
2. Replace the `local_server_udp` computation:
   ```python
   # Before:
   local_server_udp = (
       f"{DEVICE_ID}:{STREAM_PROXY_PORT}" if role == "server"
       else f"127.0.0.1:{STREAM_PROXY_PORT}"
   )
   # After:
   local_server_udp = f"{DEVICE_ID}:{INGEST_UDP_PORT}" if role == "server" else ""
   ```
   (Edge sidecar has no local inference target to deliver to on the forward path.)

**`lifespan()` — strip all proxy machinery:**
- Remove `stream_proxy_manager = StreamProxyManager()`
- Remove the entire `if DEVICE_TYPE == "edge":` block that sets up `poll_task`, `proxy_task`, `bandwidth_task`
- Remove the three `await _cancel_task(...)` calls and `stream_proxy_manager.close()`

**`deploy()` — two edits:**
1. Remove the edge-only lines:
   ```python
   container_env["STREAM_PROXY_HOST"] = STREAM_PROXY_HOST
   container_env["STREAM_PROXY_PORT"] = str(STREAM_PROXY_PORT)
   ```
2. Flatten the `if STREAM_TRANSPORT == "quic":` block — drop the conditional, keep the body unchanged (always runs). Update `STREAM_PROXY_PORT` reference inside to `INGEST_UDP_PORT`:
   ```python
   container_env["FEED_LISTEN_PORT"] = str(INGEST_UDP_PORT)
   ```

---

## `docker-compose.yml`

**Edge daemons — remove TCP port exposures** (dead proxy ports):
```yaml
# Remove from daemon-edge1, daemon-edge2, daemon-edge3:
- "9000:9000/tcp"
- "9001:9000/tcp"
- "9002:9000/tcp"
```

**All daemons — remove `STREAM_TRANSPORT=quic`** (env var no longer exists in code).

---

## Verification

```bash
# 1. Confirm nothing imports the deleted modules
grep -r "stream_proxy_manager\|tcp_utils\|StreamProxyManager\|handle_tcp_stream\|STREAM_TRANSPORT\|STREAM_PROXY_PORT\|STREAM_PROXY_HOST\|SentRateBackend\|StreamBedTCPSender" \
  control-plane/ shared/ tests/ --include="*.py"

# 2. Build all images
docker compose build

# 3. Run unit tests (no Docker needed)
pytest tests/unit/ -m unit -v

# 4. Run QUIC integration tests (requires docker compose up)
docker compose up -d
pytest tests/quic/ -m integration_quic -v

# 5. Smoke-check failure detection (was broken by sidecar prefix bug — now fixed)
pytest tests/test_failure_detection_docker.py -v
```

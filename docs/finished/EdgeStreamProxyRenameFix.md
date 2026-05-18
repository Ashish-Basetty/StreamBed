# Fix Plan: `test_dg_total_grows_on_both_sides`

Currently `xfail`-ed in [tests/gcp/test_actn_roundtrip_latency.py](../tests/gcp/test_actn_roundtrip_latency.py). The test deploys an edge+server pair on GCP, waits for the QUIC sidecars to handshake, then asserts that `dg_total` grows on both sides over a 10s window. It doesn't — `dg_total` stays at 0 on the edge side because the edge container never starts sending frames.

## Symptom

After a successful `/deploy`:
- Both daemons report `sidecar_host_ip:port` populated → sidecars came up correctly.
- Edge sidecar log: `edge: QUIC connected to 10.10.0.5:7071` → QUIC handshake completed across the VPC.
- Edge sidecar metrics every 10s: `dg_sent=0 dg_recv=0 ... stream_sent=0 stream_recv=304` → only control-channel bytes, no datagrams ever sent.
- Edge inference container logs end at `[Edge] Model loaded.` (visible after model download) — nothing after.

`docker exec edge-container env` shows:
```
VIDEO_SERVER_HOST=10.10.0.2
VIDEO_SERVER_PORT=9200
SIDECAR_HOST=streambed-gcp-test-edge-01-sidecar
SIDECAR_UDP_PORT=9050
SIDECAR_FEED_PORT=9050
```
Notably absent: `STREAM_PROXY_HOST`, `STREAM_PROXY_PORT`.

## Root cause

The edge inference container reads its sidecar address from the wrong env vars. Two sides of the contract disagree on the names:

| Side | Sets / reads | Where |
|---|---|---|
| Daemon (sets on inference container) | `SIDECAR_HOST`, `SIDECAR_UDP_PORT`, `SIDECAR_FEED_PORT` | [control-plane/DeploymentDaemon/main.py:408-411](../control-plane/DeploymentDaemon/main.py#L408) |
| Edge container (reads) | `STREAM_PROXY_HOST`, `STREAM_PROXY_PORT` | [edge/edge_config.py:12-13](../edge/edge_config.py#L12), [edge/app.py:21](../edge/app.py#L21) |

The lifespan hook at [edge/app.py:133](../edge/app.py#L133) calls `_connect_to_proxy_with_retry()`, which loops at [edge/app.py:110-122](../edge/app.py#L110):

```python
while True:
    if not (STREAM_PROXY_HOST and STREAM_PROXY_HOST.strip()):
        print("[Edge] STREAM_PROXY_HOST not set, waiting...")
        await asyncio.sleep(CONNECT_RETRY_INTERVAL)
        continue
    ...
```

Since `STREAM_PROXY_HOST` is never set, the loop spins forever printing "STREAM_PROXY_HOST not set, waiting..." — but that line never reaches `docker logs` because `print()` against a pipe is block-buffered in Python and the buffer never flushes (no `flush=True`, no `PYTHONUNBUFFERED=1`). End result: silent infinite wait. The `capture_task` (line 134) never starts. No frames ever leave the edge container.

## Why it was introduced

`git log -S "STREAM_PROXY_HOST"` shows commit **`350a662` ("Removed UDP code", 2026-05-11)** stripped the entire UDP-proxy plumbing out of the daemon when switching the transport to the QUIC sidecar. That commit removed:

- `daemon_config.STREAM_PROXY_HOST` / `STREAM_PROXY_PORT`
- The daemon's path that set those env vars on the inference container
- `STREAM_TRANSPORT` switch

…and replaced them with `SIDECAR_HOST` / `SIDECAR_UDP_PORT` / `SIDECAR_FEED_PORT`. The edge container code was not updated in the same commit. The local docker-compose integration tests didn't catch this because no local test asserts on `dg_total` growth — they assert on registration, deploy, and routing-table content, all of which work without any actual frame flow.

## Why this didn't break the QUIC handshake test

The QUIC handshake test (`test_cross_vm_quic_handshake.py`) asserts `sidecar_host_ip` + `sidecar_host_port` are populated in the controller's `/status`. Those endpoints are allocated by the daemon at `/deploy` time and bootstrapped into `device_status` by the controller — they don't depend on anything actually flowing. So the QUIC handshake test passes even when the edge container never sends. That's actually a separate weakness in the QUIC test (it's a false positive risk) — see "Bonus tightening" at the end.

## Fix

Two viable directions; pick one.

### Option A (recommended): rename edge to `SIDECAR_*`

Align the edge with the rest of the codebase. `SIDECAR_*` is the convention used by daemon, sidecar (Go), `daemon_config.py`, and controller comments. `STREAM_PROXY_*` is only in `edge/`.

**Touch list**:
1. [edge/edge_config.py:12-13](../edge/edge_config.py#L12) — rename:
   ```python
   SIDECAR_HOST = os.getenv("SIDECAR_HOST", "")
   SIDECAR_UDP_PORT = int(os.getenv("SIDECAR_UDP_PORT", "9050"))
   ```
2. [edge/app.py](../edge/app.py) — replace `STREAM_PROXY_HOST` → `SIDECAR_HOST`, `STREAM_PROXY_PORT` → `SIDECAR_UDP_PORT` (4 references at lines 21, 113-121).
3. Update any local docker-compose env vars that reference `STREAM_PROXY_*` (grep first; current `docker-compose.yml` doesn't appear to).
4. Rebuild + push `ashishbasetty/streambed-edge:latest` (linux/amd64).

**Risks**: tiny. No other code reads `STREAM_PROXY_HOST` (`grep -rn STREAM_PROXY` shows only edge files).

### Option B: rename daemon to `STREAM_PROXY_*`

Keep edge alone. Make the daemon set the older names.

**Touch list**:
1. [control-plane/DeploymentDaemon/main.py:408-411](../control-plane/DeploymentDaemon/main.py#L408) — change `SIDECAR_HOST` → `STREAM_PROXY_HOST`, `SIDECAR_UDP_PORT` → `STREAM_PROXY_PORT`.

**Why not**: fights the dominant naming convention. The sidecar Go code, daemon's own config, and surrounding logs all say "sidecar." Renaming back to "stream proxy" reintroduces vocabulary the May 11 refactor explicitly removed.

### Option C: add a back-compat shim

Set both names in the daemon. Edge keeps reading `STREAM_PROXY_*`.

**Why not**: locks in two-name confusion forever. A shim is appropriate when you have downstream consumers you can't update; we control all the code here.

## Verification

1. Run the QUIC handshake test (should still pass after the fix — it doesn't care about flow):
   ```
   pytest -m gcp tests/gcp/test_cross_vm_quic_handshake.py -v
   ```
2. Run the ACTN flow test (was xfail; should now xpass and then need the marker removed):
   ```
   pytest -m gcp tests/gcp/test_actn_roundtrip_latency.py -v
   ```
   With `strict=False` it'll log `XPASS` not fail. Once observed green, **remove the `@pytest.mark.xfail` decorator** so future regressions show as red rather than silent xfail/xpass.
3. Manual sanity check on a worker after re-deploy:
   ```
   gcloud compute ssh edge-01 --zone=us-central1-a --tunnel-through-iap \
     --command='sudo docker logs --tail 20 streambed-gcp-test-edge-01-sidecar | grep dg_sent'
   ```
   Expect `dg_sent` to be growing within ~30s of model load completing.

## Local test coverage gap to consider closing

The local docker-compose integration suite passed across this regression because nothing in it asserts on `dg_total` or downstream frame consumption. Worth adding a thin local test that does the same `dg_total > 0` assertion against the local compose stack so future "removed UDP code"-style refactors get caught before they reach GCP. Out of scope for this fix but worth tracking.

## Bonus tightening (optional follow-up)

`test_cross_vm_quic_handshake` currently passes whenever the daemon allocates a sidecar host endpoint, even if the sidecar container fails to spawn (we hit this earlier with the arm64-only sidecar image — the test passed despite zero actual sidecars). To make it a real handshake test, also poll for a sidecar heartbeat with non-null `dg_total` (proves the sidecar is *alive* and reporting, not just that the daemon reserved a port). One additional predicate, ~5 lines.

## Order of operations

1. Apply Option A to the edge code.
2. `docker buildx build --platform linux/amd64 -t ashishbasetty/streambed-edge:latest -f edge/Dockerfile --push .`
3. Restart the GCP cluster + redeploy controller stack (worker stacks don't need a restart; they pull on next deploy).
4. Re-run `pytest -m gcp tests/gcp/test_actn_roundtrip_latency.py -v`.
5. If green: remove `@pytest.mark.xfail` from the test and the long reason string.
6. Commit edge rename + test marker removal together.

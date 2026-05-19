# Fix Plan: `retry_count` never resets in `device_status`

## Symptom

`retry_count` in [control-plane/ControllerNode/streambed_controller/db.py](../control-plane/ControllerNode/streambed_controller/db.py) only grows. After a device recovers — heartbeats arrive, `inference_alive=true`, status transitions back to `ACTIVE` — `retry_count` stays at whatever it climbed to. The next time the same device hits a *single* transient failure, the health monitor reads the old `retry_count` and computes a huge cooldown:

```python
# health_monitor.py:146-150
def _restart_delay_for_retry(self, retry_count: int) -> timedelta:
    base_secs = self.restart_cooldown.total_seconds()   # 5
    delay_secs = min(base_secs * (2 ** retry_count), 600)
    return timedelta(seconds=delay_secs)
```

| `retry_count` | cooldown |
|---:|---:|
| 1 | 10s |
| 2 | 20s |
| 3 | 40s |
| 4 | 80s |
| 5 | 160s |
| 6 | 320s |
| 7+ | 600s (cap) |

## How we noticed

[tests/test_failure_detection_docker.py](../tests/test_failure_detection_docker.py) kills inference containers three times in a row across three tests, sharing the session-scoped `deployed_inference_stack`. By the third test, `retry_count` for `edge-001` / `server-001` reaches 3–5. The next test in the same session ([tests/test_integration_dg_flow.py](../tests/test_integration_dg_flow.py)) inherits that state. When *one* container needs even a single restart during dg_flow's 120s warmup, the cooldown is already 40–160s and the test times out.

## Root cause

[db.py:200-220](../control-plane/ControllerNode/streambed_controller/db.py#L200) only ever increments the counter — there is no reset path:

```python
def set_device_status_evaluated(
    device_cluster: str, device_id: str, status: str, increment: bool = False,
) -> None:
    increment_str = " + 1" if increment else ""
    conn.execute(
        f"""
        UPDATE device_status
        SET status = ?, retry_count = COALESCE(retry_count, 0){increment_str}
        WHERE device_cluster = ? AND device_id = ?
        """, (status, device_cluster, device_id),
    )
```

`increment=True` is passed from [health_monitor.py:226](../control-plane/ControllerNode/streambed_controller/health_monitor.py#L226) when the device transitions to `UNRESPONSIVE`. There is no symmetric call on the `ACTIVE` branch — `retry_count` is treated as cumulative-forever, not as "consecutive failures".

The cooldown formula assumes the variable means **consecutive failures since last recovery**. The code stores **all failures since the row was created**. The mismatch is the bug.

## Fix

Reset `retry_count` to 0 when status transitions from a failure state (`UNRESPONSIVE` / device-dead) to `ACTIVE` on a fresh heartbeat. Two equivalent places to do it; option A is cleaner.

### Option A (recommended): reset inside `update_sidecar_heartbeat`

`update_sidecar_heartbeat` already runs on every heartbeat and is the only place that proves a sidecar is fresh-and-alive. Have it set `retry_count = 0` as part of the same UPDATE.

[db.py:441-450](../control-plane/ControllerNode/streambed_controller/db.py#L441) (the UPSERT inside `update_sidecar_heartbeat`):

```python
INSERT INTO device_status (..., sidecar_host_ip, sidecar_host_port, dg_total, data_flow_state, ...)
VALUES (...)
ON CONFLICT(device_cluster, device_id) DO UPDATE SET
    sidecar_host_ip   = excluded.sidecar_host_ip,
    sidecar_host_port = excluded.sidecar_host_port,
    dg_total          = excluded.dg_total,
    ...
    retry_count       = 0,           -- ADD: heartbeat proves recovery
```

**Why this is correct:** A heartbeat means the sidecar process is up AND the daemon answered `/inference-status`. The thing the `retry_count` was tracking — "how many times in a row did we fail to bring this device back" — is by definition zero once we hear from it again.

**Risks:** Tiny. If the heartbeat lies (e.g., sidecar is alive but inference container is dead — `inference_alive=false`), we'd reset prematurely. To avoid that, gate the reset on `inference_alive=true`:

```python
retry_count = CASE WHEN excluded.inference_alive THEN 0 ELSE retry_count END,
```

That keeps backoff intact while inference is genuinely down, but resets it cleanly the moment the container reports alive.

### Option B: reset in `set_device_status_evaluated` when status flips to ACTIVE

Add a `reset: bool = False` parameter and call it from the health monitor's ACTIVE branch ([health_monitor.py:222-223](../control-plane/ControllerNode/streambed_controller/health_monitor.py#L222)):

```python
states[device_id] = "ACTIVE"
set_device_status_evaluated(cluster, device_id, HeartbeatStatus.ACTIVE, reset=True)
```

with `set_device_status_evaluated` running `retry_count = 0` instead of `+ 1`. Why not this: status transitions can flap (UNRESPONSIVE → ACTIVE → UNRESPONSIVE within a few seconds during a restart cascade). Resetting on every ACTIVE evaluation can erase legitimate backoff mid-cascade.

Option A only resets when we actually receive a heartbeat from the device — that's a stronger signal than "the evaluation loop happened to see ACTIVE this tick".

## Test coverage

Add to [tests/unit/test_sidecar_heartbeat_db.py](../tests/unit/test_sidecar_heartbeat_db.py):

1. **`test_heartbeat_resets_retry_count`** — seed `device_status` with `retry_count = 5`, call `update_sidecar_heartbeat(...)` with `inference_alive=true`, assert `retry_count = 0`.
2. **`test_heartbeat_preserves_retry_count_when_inference_dead`** — same seed, call with `inference_alive=false`, assert `retry_count` unchanged. (Only if we adopt the `CASE WHEN inference_alive` guard.)
3. **`test_unresponsive_increments_retry_count`** — already partially covered; confirm it still works after the change.

## Out of scope

- Capping retry_count at some max even without recovery. The 10-minute cap on cooldown duration already provides the bound that matters; the counter can climb arbitrarily without consequence.
- Surfacing `retry_count` in `/status` for tests/operators. Optional follow-up; not load-bearing for this fix.

## Order of operations

1. Apply Option A to `update_sidecar_heartbeat` (one line in the UPSERT).
2. Add the two unit tests in `test_sidecar_heartbeat_db.py`.
3. Run `pytest tests/unit/test_sidecar_heartbeat_db.py -v`.
4. Re-run the full local suite — `test_failure_detection_docker` followed by `test_integration_dg_flow` should pass on a single invocation without the test 3 redeploy hack that's currently masking the issue.
5. Once green, consider removing the redeploy at the end of [`test_controller_stays_running_during_failures`](../tests/test_failure_detection_docker.py) — it was added as a workaround; with `retry_count` resetting on recovery, the health monitor will handle reclaim within the next test's warmup window on its own.

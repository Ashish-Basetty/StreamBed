# Plan: Move heartbeat to the sidecar + slim the deployments row

## Context

We just finished the per-device networks migration (the prior content of this
plan file is now [docs/PerDeviceNetworksPlan.md](../../Code/CS214/StreamBed/docs/PerDeviceNetworksPlan.md)).
That work added `sidecar_host_ip` / `sidecar_host_port` as `NOT NULL`
columns on `deployments`, written from the daemon's `/deploy` response.

Two things bother us about that landing point:

1. **Storing dynamically-allocated ports in `deployments` is a category
   mismatch.** `deployments` should describe deploy *intent* (image, host
   port mapping requested at deploy time). The sidecar's host UDP port is
   chosen at runtime by the daemon and can change if state is lost.
   Mixing intent and runtime state in one row makes drift invisible.
2. **Heartbeats today come from the inference container**
   ([shared/heartbeat.py:18](../../Code/CS214/StreamBed/shared/heartbeat.py#L18)),
   which violates the project's abstraction boundary ("inference containers
   must stay unaware of transport details" — `project_streambed` memory).
   It also gives a weak liveness signal: an inference process can be
   "Active" while the data plane has stopped flowing — no frames in, no
   actions out, but the heartbeat keeps ticking.

The sidecar is the natural heartbeater: it's the data-plane chokepoint, it
already owns the host UDP port mapping, and it has live counters
([metrics.go:15-25](../../Code/CS214/StreamBed/sidecar/internal/metrics/metrics.go#L15-L25))
that can distinguish "process alive" from "actually moving data." Moving
the heartbeat to the sidecar both restores the abstraction boundary and
lets the controller detect the *active-but-idle* failure mode.

## Goal

- Sidecar heartbeats the controller every N seconds with `{cluster,
  device_id, role, sidecar_host_ip, sidecar_host_port, metrics_snapshot}`.
- Controller persists endpoint + activity in `device_status` (heartbeat-
  derived) and clears those fields out of `deployments`.
- Inference container stops calling the controller; `shared/heartbeat.py`
  goes away.
- Deployment row also drops other dead fields surfaced by the audit.

## Deployments row audit

Current columns ([db.py:70-87](../../Code/CS214/StreamBed/control-plane/ControllerNode/streambed_controller/db.py#L70-L87)):

| Column | Verdict |
| --- | --- |
| `device_cluster`, `device_id` | KEEP — primary key |
| `device_type` | KEEP — used by `_attempt_restart` to redeploy with the right type |
| `image` | KEEP — deploy intent |
| `host_port`, `container_port` | KEEP — inference container's port mapping, also deploy intent |
| `container_hash` | KEEP — matches a deployment row to a live Docker container (orphan recovery) |
| `container_name` | KEEP — `/delete` API forwards this to the daemon ([deploy.py:113-114](../../Code/CS214/StreamBed/control-plane/ControllerNode/streambed_controller/deploy.py#L113-L114)); convenient even though derivable |
| `managing_daemon_id` | **DROP** — always equals `device_id` ([deploy.py:71](../../Code/CS214/StreamBed/control-plane/ControllerNode/streambed_controller/deploy.py#L71)). Dead. |
| `sidecar_name` | **DROP** — fully derivable from `f"streambed-{cluster}-{device_id}-sidecar"` ([sidecar_supervisor.py:19-20](../../Code/CS214/StreamBed/control-plane/DeploymentDaemon/sidecar_supervisor.py#L19-L20)). Daemon's `/delete` derives it the same way if not passed. |
| `sidecar_host_ip`, `sidecar_host_port` | **DROP** — heartbeat-managed, lives in `device_status` |
| `status`, `deployed_at` | KEEP |

Result: deployments becomes a focused "what was requested" record.

## device_status extensions

Add heartbeat-derived columns ([db.py:49-57](../../Code/CS214/StreamBed/control-plane/ControllerNode/streambed_controller/db.py#L49-L57)):

| Column | Notes |
| --- | --- |
| `sidecar_host_ip TEXT` | nullable until first heartbeat (or bootstrap from /deploy) |
| `sidecar_host_port INTEGER` | nullable until first heartbeat |
| `dg_total INTEGER` | sum of `dg_sent + dg_recv` from last heartbeat — used to detect activity |
| `data_flow_state TEXT` | `"flowing"` if dg_total increased since prior heartbeat; `"idle"` if unchanged; `"unknown"` if no prior |

Bootstrap path stays: daemon `/deploy` response still returns
`{sidecar_host_ip, sidecar_host_port}`; controller writes those into
`device_status` once at deploy time so the first edge dial doesn't wait
for the first sidecar heartbeat. After that the heartbeat is authoritative.

## Sidecar heartbeat (Go)

New file `sidecar/internal/heartbeat/heartbeat.go`. One function:

```go
func Loop(ctx context.Context, cfg Config) // sends every cfg.Interval
```

Config struct fields (mirrors what env brings in via main.go):
- `ControllerURL`, `Cluster`, `DeviceID`, `Role`
- `HostIP`, `HostPort` (echoed from `DAEMON_PUBLIC_IP` / `SIDECAR_HOST_PORT` env, passed in by the daemon when it spawns the sidecar)
- `Interval time.Duration`
- `Reg *metrics.Registry` (read snapshot of counters per tick)

Pattern: copy the existing `pollDaemonTarget` shape in
[edge.go:154-189](../../Code/CS214/StreamBed/sidecar/internal/edge/edge.go#L154-L189) —
`http.Client{Timeout: 5s}`, `time.NewTicker`, select on `ctx.Done`. POST
JSON. Log on failure, do not abort.

Spawned from [main.go](../../Code/CS214/StreamBed/sidecar/cmd/streambed-quic-sidecar/main.go)
alongside `reg.LogLoop` (line 38) — works the same for both `edge` and
`server` roles, so it lives in main, not in `edge.Run`/`server.Run`.

Counters read into the heartbeat are the same ones already logged by
`LogLoop` ([metrics.go:61-77](../../Code/CS214/StreamBed/sidecar/internal/metrics/metrics.go#L61-L77)):
`dg_sent + dg_recv` summed into one `dg_total` to keep the schema small.
(Per-counter detail can be added later if we need to distinguish ingress
vs egress idle.)

## Controller endpoint

New: `POST /sidecar-heartbeat` in
[controller/main.py](../../Code/CS214/StreamBed/control-plane/ControllerNode/streambed_controller/main.py).

Request model:
```python
class SidecarHeartbeatRequest(BaseModel):
    device_cluster: str
    device_id: str
    role: str            # "edge" | "server"
    sidecar_host_ip: str
    sidecar_host_port: int
    dg_total: int        # cumulative datagrams seen (sent + recv)
```

Handler logic:
1. Compute `data_flow_state` by diffing against stored `dg_total` (new >
   old → "flowing"; equal → "idle"; no prior → "unknown").
2. Upsert `device_status` row: refresh `last_heartbeat`, write the
   endpoint + counters + `data_flow_state`.
3. If endpoint changed vs stored value, call `_push_target_to_routed_edges`
   for this device (re-uses the helper added in the prior migration —
   [deploy.py](../../Code/CS214/StreamBed/control-plane/ControllerNode/streambed_controller/deploy.py)).
4. Return `{"ok": true}`.

Existing `/heartbeat` endpoint stays for now (other callers may exist).
Deleting `shared/heartbeat.py` removes the only known caller from
inference containers.

## Files to modify

| File | Change |
| --- | --- |
| [sidecar/cmd/streambed-quic-sidecar/main.go](../../Code/CS214/StreamBed/sidecar/cmd/streambed-quic-sidecar/main.go) | Read `CONTROLLER_URL`, `DEVICE_CLUSTER`, `DEVICE_ID`, `SIDECAR_HOST_IP`, `SIDECAR_HOST_PORT`, `HEARTBEAT_INTERVAL` env vars. Spawn `heartbeat.Loop` for both roles. |
| `sidecar/internal/heartbeat/heartbeat.go` (NEW) | Ticker loop modeled on `pollDaemonTarget`; POST JSON to `CONTROLLER_URL/sidecar-heartbeat`. |
| [control-plane/DeploymentDaemon/sidecar_supervisor.py](../../Code/CS214/StreamBed/control-plane/DeploymentDaemon/sidecar_supervisor.py) | Pass `CONTROLLER_URL`, `DEVICE_CLUSTER`, `DEVICE_ID`, `SIDECAR_HOST_IP`, `SIDECAR_HOST_PORT`, `HEARTBEAT_INTERVAL` env into the spawned sidecar. `host_udp_port` is already plumbed; reuse it for `SIDECAR_HOST_PORT`. |
| [control-plane/DeploymentDaemon/main.py](../../Code/CS214/StreamBed/control-plane/DeploymentDaemon/main.py) | Drop `SIDECAR_HOST` injection into inference env (line 373) — inference no longer dials the sidecar by name for heartbeat purposes; the in-device DNS lookup still works for the data-plane TCP shim, so verify if it's still needed for that. If yes keep; if only for heartbeat, drop. |
| [control-plane/ControllerNode/streambed_controller/db.py](../../Code/CS214/StreamBed/control-plane/ControllerNode/streambed_controller/db.py) | `deployments` schema: drop `managing_daemon_id`, `sidecar_name`, `sidecar_host_ip`, `sidecar_host_port`. `device_status` schema: add `sidecar_host_ip TEXT`, `sidecar_host_port INTEGER`, `dg_total INTEGER`, `data_flow_state TEXT`. Update `record_deployment`, `get_last_deployment`, `get_cluster_deployments` accordingly. New helper `update_sidecar_heartbeat(cluster, device_id, ip, port, dg_total) -> tuple[str, str|None]` returning `(data_flow_state, prior_endpoint_or_none)` so the endpoint can diff and re-push. |
| [control-plane/ControllerNode/streambed_controller/deploy.py](../../Code/CS214/StreamBed/control-plane/ControllerNode/streambed_controller/deploy.py) | Stop passing `sidecar_host_ip` / `sidecar_host_port` to `record_deployment`. Instead, after recording, write the endpoint into `device_status` as the bootstrap. Keep `_push_target_to_routed_edges` for the server-deploy immediate-push path. |
| [control-plane/ControllerNode/streambed_controller/health_monitor.py](../../Code/CS214/StreamBed/control-plane/ControllerNode/streambed_controller/health_monitor.py) | `_stream_target_endpoint` reads from `device_status` instead of `deployments`. |
| [control-plane/ControllerNode/streambed_controller/main.py](../../Code/CS214/StreamBed/control-plane/ControllerNode/streambed_controller/main.py) | New `POST /sidecar-heartbeat` endpoint and `SidecarHeartbeatRequest` model. Update `/deployments` listing to drop the gone columns. |
| [shared/heartbeat.py](../../Code/CS214/StreamBed/shared/heartbeat.py) | **Delete**. Inference no longer heartbeats. |
| [experiments/advisor/server/advisor_server.py](../../Code/CS214/StreamBed/experiments/advisor/server/advisor_server.py), [experiments/advisor/edge/edge_inference.py](../../Code/CS214/StreamBed/experiments/advisor/edge/edge_inference.py) | Remove the `heartbeat_loop()` import and the `asyncio.create_task(heartbeat_loop(...))` call. |
| [tests/test_advisor_smoke.py](../../Code/CS214/StreamBed/tests/test_advisor_smoke.py) | No required change — assertions operate on sidecar metric lines, which still flow. Optionally add an assertion that `device_status` shows `data_flow_state="flowing"` after frame_gen runs. |

## Caveats

- **Inference liveness regression.** Today if inference dies, the
  controller marks the device UNRESPONSIVE and `_attempt_restart` kicks
  in. After this change, if sidecar lives but inference dies, the
  controller sees "Active" with `data_flow_state="idle"` — no restart is
  triggered. This is a real reduction in auto-recovery for one specific
  failure. Tracked as out-of-scope; add a restart trigger on sustained
  `"idle"` later if it bites.
- **Schema churn on a recently-changed schema.** We just added
  `sidecar_host_*` to `deployments` as `NOT NULL`. Removing them right
  after lands is fine because we wiped `controller.db` last bring-up;
  document the wipe again in the verification steps.
- **`SIDECAR_HOST` env on the inference container** — used by the
  inference Python to dial the sidecar's *in-device* TCP shim. Keep it
  unless audit shows it's only used for the heartbeat path. (Quick grep
  during execution.)

## Verification

1. `rm control-plane/data/controller.db*` (manual; user runs this).
2. `docker compose up -d controller router daemon-edge1 daemon-server1`.
3. `pytest tests/test_advisor_smoke.py` — must still pass: QUIC connects,
   sidecar metrics non-zero, `total_advised > 0`.
4. After frame_gen runs:
   `curl http://localhost:8080/status` should show
   `data_flow_state="flowing"` for both edge-001 and server-001.
5. Negative: stop sending frames, wait one heartbeat interval, observe
   `data_flow_state` flips to `"idle"`.
6. `curl http://localhost:8080/deployments` returns rows *without* the
   `sidecar_*` columns; schema slim confirmed.
7. Inference logs do **not** contain `heartbeat_loop` lines anymore;
   `shared/heartbeat.py` is gone.

## Out of scope

- Auto-restart on sustained `data_flow_state="idle"` with a deployed
  inference container.
- Per-direction counters in heartbeat (`dg_sent` and `dg_recv` separate)
  — useful for diagnosing one-way breakage; defer until needed.
- Removing the existing `/heartbeat` endpoint and its `HeartbeatRequest`
  model. Leave for now in case something still calls it; deletable in a
  follow-on once we confirm no callers.

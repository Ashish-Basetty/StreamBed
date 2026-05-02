# Routing Workflow

This describes how edges get assigned to servers, end-to-end.

## 1. Data model

`routing` table ([db.py:49](../controller/ControllerNode/db.py#L49)):

| column           | meaning                                          |
| ---------------- | ------------------------------------------------ |
| `source_cluster` | cluster name (PK part 1)                         |
| `source_device`  | edge `device_id` (PK part 2)                     |
| `target_cluster` | currently always equals `source_cluster`         |
| `target_device`  | server `device_id` the edge is assigned to       |
| `updated_at`     | last time the assignment changed                 |

PK is `(source_cluster, source_device)` — every edge has at most one route. Servers don't appear as a `source_device`. There is no schema-level FK to the `devices` table, so rows can outlive the device.

## 2. Shared assignment primitives

All paths that mutate `routing` go through three helpers in [routing.py](../controller/ControllerNode/routing.py):

- **`assign_edge_to_least_loaded_server(cluster, edge_id)`** — picks the server in `cluster` with the fewest existing routing rows pointing at it (load measured by *count of assignments*) and UPSERTs the edge's row. Returns the assigned server id, or `None` if the cluster has no servers.
- **`assign_unrouted_edges(cluster)`** — finds every edge in the cluster with no routing row and runs the assigner above on each. Returns the list of edges that got newly placed.
- **`orphan_edges_for_server(cluster, failed_server)`** — deletes every routing row in `cluster` whose `target_device` is `failed_server`. Returns the list of orphaned edge ids.

These functions are pure DB operations — no network pushes. The caller is responsible for telling edges to switch (see §5a).

## 3. Initial fill on controller startup

In the FastAPI `lifespan` hook ([main.py:33-62](../controller/ControllerNode/main.py#L33-L62)):

1. Read every distinct cluster from `devices`.
2. For each cluster, collect `servers` and `edges`.
3. Skip if no servers.
4. Pick `target_server = servers[0]` and INSERT a routing row for each edge that has no existing routing row.

(This older one-shot backfill predates the shared helpers in §2 and uses naive "first server" rather than least-loaded. Replacing the body with a call to `assign_unrouted_edges(cluster)` would unify it with the rest of the pipeline.)

## 4. Registration path

`POST /register` ([main.py:132-142](../controller/ControllerNode/main.py#L132-L142)) writes/updates the `devices` row, then branches on `device_type`:

- **Edge registers** → `assign_edge_to_least_loaded_server(cluster, edge_id)`.
- **Server registers** → `assign_unrouted_edges(cluster)`.

## 5. Failover path

The health monitor runs a single periodic routing pass every `check_interval` (default 5s) once the startup grace window has elapsed. The pass is implemented as `_routing_tick` ([health_monitor.py](../controller/ControllerNode/health_monitor.py)) and is the **only** routing-related periodic work the controller does — failover detection and the bulk stream-target safety-net push are both inside it. See §6 for the safety-net half.

The health monitor treats a server as **failed for routing purposes** when *either* of these is true:

1. The server is `UNRESPONSIVE` — its last heartbeat is older than `heartbeat_timeout`.
2. The server has **no row in the `deployments` table**. This is the same "No Model Deployed" condition the frontend surfaces as the purple badge: the controller never recorded a successful `/deploy` for it (or the last deployment was deleted via `/delete`). See §5d for the correspondence with the frontend.

In either case, the server is unsuitable as a routing target — `UNRESPONSIVE` means no traffic will get through, and "no deployment" means there is nothing to receive the traffic. The handling is identical: orphan the failed server's edges and run the registration path on them.

### 5a. Detecting and reassigning

`_process_cluster_health` ([health_monitor.py](../controller/ControllerNode/health_monitor.py)):

1. Compute per-device states (`ACTIVE`, `UNRESPONSIVE`, `UNKNOWN`) from heartbeat freshness.
2. Load `deployed = get_cluster_deployments(cluster)` — the set of devices with a deployment row.
3. Compute `healthy_servers = {s : state == ACTIVE and s in deployed}`.
4. For each server in the cluster:
   - If `state == UNRESPONSIVE`, reason = `"unresponsive"`.
   - Else if `s not in deployed`, reason = `"no model deployed"`.
   - Else: skip — the server is fine.
5. If `healthy_servers` is empty, log a warning and skip (edges keep their old target — the situation may resolve).
6. Otherwise, for the failed server:
   1. **`orphan_edges_for_server(cluster, failed_server)`** — delete the failed server's routing rows. Affected edges now have no routing entry.
   2. **`assign_unrouted_edges(cluster)`** — greedy least-loaded reassignment across the remaining servers, identical to the path `/register` takes. Each orphaned edge gets a fresh routing row.
   3. **`_push_targets_for_edges(cluster, orphaned)`** — for each previously-orphaned edge, resolve its new `target_device` to an IP and PUT to that edge daemon's `/stream-target`.

### 5b. Eligibility filter in the assigner

`assign_edge_to_least_loaded_server` ([routing.py](../controller/ControllerNode/routing.py)) **filters its candidate server list by deployment presence** — it joins `devices` against `deployments` and only considers servers with a row in the latter. Without this filter the failover loop would thrash: it would orphan an edge from a no-model server only for the assigner to immediately route it back as the least-loaded option.

This means a server with no model deployed is invisible to the placement algorithm regardless of its heartbeat state. As soon as a deployment lands (via `/deploy` → `record_deployment`), the server becomes eligible for new assignments at the next call into the assigner.

### 5c. Why the merge with §4 matters

Failover and "a new server registered, distribute orphans to it" are now the same operation: take a set of edges with no routing rows and place them via the same load-aware function. This means:

- Edges already routed to a healthy server are **not** disturbed — only the ones whose target failed move.
- The greedy least-loaded assigner naturally distributes orphans across multiple healthy servers instead of dumping them all on `healthy_servers[0]`.
- One code path to test, one place to change the placement policy.

### 5d. Correspondence with the frontend "No Model Deployed" badge

The frontend surfaces the same "no deployment" condition as a purple badge labeled `No Model Deployed: <heartbeat time>`. The chain is:

1. Controller's `GET /deployments?device_cluster=<c>` ([main.py](../controller/ControllerNode/main.py)) returns the rows in the `deployments` table.
2. The frontend's `app.js` calls this endpoint on each refresh and decorates each device with `deployed_image` ([app.js](../frontend/app.js) — `decorate()` and `deployByDevice` map).
3. `hasModel(d)` returns true iff `d.deployed_image` is set.
4. The template renders the purple badge in the `!hasModel(d)` branch ([index.html](../frontend/index.html)).

So a server showing the purple "No Model Deployed" badge in the UI is exactly a server the failover logic considers failed. They are reading the same state from the same table.

### 5e. Recovery is not automatic

Once an edge has been moved off a failed server, it stays on its new target even if the original server comes back (heartbeats again, or has a model deployed). The recovered server is treated like any other healthy server — it'll only receive new assignments from future registrations or failovers.

## 6. Periodic stream-target sync (safety net)

The bulk re-push of stream-targets is the second half of `_routing_tick`. It runs on a slower cadence than failover detection: every tick checks whether `stream_target_sync_interval` (default 30s) has elapsed since the last bulk push, and only then runs `_sync_stream_targets_from_routing`. Both halves share the same loop and lock-step ordering — failover handling for all clusters happens first, then the bulk push (if due).

`_sync_stream_targets_from_routing` ([health_monitor.py](../controller/ControllerNode/health_monitor.py)):

1. Read every row in `routing`.
2. For each row, look up `target_device`'s IP.
3. PUT `{target_ip, target_port: 9000}` to `http://<edge_ip>:<daemon_port>/stream-target`.

The daemon writes that to `stream-target.json`; the running container polls the file and updates its proxy destination ([DeploymentDaemon/main.py:117](../controller/DeploymentDaemon/main.py#L117), [server/app.py:101](../server/app.py#L101)).

This sync is the safety net: even if the per-failover `_push_targets_for_edges` call drops on the floor, the next periodic bulk sync re-pushes the correct target. The routing table is authoritative.

## 7. Deregistration

`POST /deregister` ([main.py:210](../controller/ControllerNode/main.py#L210)) calls `deregister_device`, which only deletes from `devices`. **Routing rows are not touched.** Consequences:

- Deregistering an *edge* leaves a stale routing row.
- Deregistering a *server* leaves edges pointing at a server that no longer exists. Those edges only get reassigned when the health monitor catches the server as `UNRESPONSIVE` and triggers §5a.

## 8. Read API

`GET /routing?device_cluster=<name>` ([main.py:295](../controller/ControllerNode/main.py)) returns raw routing rows. The frontend uses this to render edge → server arrows.

## 9. End-to-end summary

```
edge POST /register
  └─> assign_edge_to_least_loaded_server(cluster, edge)
        └─> UPSERT routing row

server POST /register
  └─> assign_unrouted_edges(cluster)
        └─> for each edge with no routing row:
              assign_edge_to_least_loaded_server

HealthMonitor._monitor_loop  (loops every check_interval after grace)
  └─> _routing_tick
        ├─> for each cluster:                              # failover (every tick)
        │     _process_cluster_health
        │       └─> server is UNRESPONSIVE OR has no row in `deployments`,
        │           AND ≥1 server is ACTIVE+deployed:
        │             orphan_edges_for_server(cluster, failed)         # DELETE rows
        │               └─> assign_unrouted_edges(cluster)             # greedy reassign
        │                     └─> only servers with a deployment row are eligible
        │                     └─> _push_targets_for_edges(cluster, …)  # PUT /stream-target
        └─> if stream_target_sync_interval has elapsed:    # safety net (slower cadence)
              _sync_stream_targets_from_routing
                └─> for every routing row: PUT /stream-target on edge daemon
```

## 10. Known limitations

- **Stale routing rows on deregister.** `/deregister` doesn't clean up `routing`. Workaround: rely on the next failover or registration to overwrite.
- **No proactive rebalance.** Once routes exist, they're sticky except on server failure. Recovered servers don't reclaim their edges.
- **No flap protection on failover.** A single missed heartbeat that flips a server to `UNRESPONSIVE` triggers immediate orphan + reassign. Adding a debounce (require N consecutive misses) would prevent unnecessary churn.
- **Cross-cluster routing is unused.** `target_cluster` always equals `source_cluster`.
- **Lifespan startup backfill (§3) bypasses the shared assigner** and uses naive "first server" placement. Should be migrated to call `assign_unrouted_edges(cluster)` for consistency.

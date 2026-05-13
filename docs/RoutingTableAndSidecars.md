# Routing table and QUIC sidecars — step-by-step

This document describes **how the controller’s `routing` table relates to QUIC sidecars**, end to end. **`routing`** stays a logical edge → server map; **QUIC dial addresses** live on **`devices`** (filled at daemon registration) and appear on **`GET /routing`** via a join. The health monitor pushes **`target_ip`** + **`quic_port`** to each edge’s deployment daemon (`/stream-target`).

## 1. What the routing table actually stores

Schema (`control-plane/ControllerNode/db.py`):

- **`source_cluster`, `source_device`** — the edge (`device_id`).
- **`target_cluster`, `target_device`** — today always same cluster; **`target_device` is the server’s `device_id`**, not a container name.
- **`updated_at`** — last assignment change.

**Sidecars are not columns in `routing`.** QUIC *dial* addresses live on **`devices`** (registration) and are **joined into `GET /routing`** mainly for **observability** (UI, debugging, future tooling). See §3.2 and §10 for what actually consumes them.

## 2. How rows are created and updated (DB only)

All mutations go through `control-plane/ControllerNode/routing.py`:

1. **`assign_edge_to_least_loaded_server(cluster, edge_id)`**
   - Builds the set of **eligible servers**: `devices` rows with `device_type='server'` **inner join** `deployments` on the same `(device_cluster, device_id)`. Servers without a deployment row are invisible to placement.
   - Counts existing `routing` rows per `target_device` in that cluster (load).
   - Picks the minimum-load server and **UPSERTs** `(source_cluster=cluster, source_device=edge_id) → target_device=that server`.
   - **No network I/O** — edge daemons are not notified here.

2. **`assign_unrouted_edges(cluster)`**
   - Every `devices` edge in the cluster **without** a `routing` row gets `assign_edge_to_least_loaded_server` called for it.
   - Returns the list of edges that **newly** received a row (caller rarely uses this for stream-target pushes).

3. **`orphan_edges_for_server(cluster, failed_server)`**
   - **DELETE**s all routing rows in the cluster whose `target_device` is that server.
   - Returns affected edge ids (used to drive immediate stream-target pushes after failover).

## 3. When those helpers run (controller entry points)

### 3.1 `POST /register` (`control-plane/ControllerNode/main.py`)

- **`sidecar_host`** and **`sidecar_quic_port`** are **required** on every register (edge and server). They describe the QUIC listener peers should dial (IP, hostname, or Docker DNS name — use **`SIDECAR_REGISTER_HOST`** on the daemon for bare metal vs default `streambed-{cluster}-{id}-sidecar` in compose).
- **Edge registers** → `assign_edge_to_least_loaded_server` (may create/update a row).
- **Server registers** → `assign_unrouted_edges` (fills rows for edges that had none).

**Gap:** Neither path calls `PUT /stream-target` on edge daemons. A **new** route can exist in SQLite for up to **`stream_target_sync_interval`** (default 30s) before `_sync_stream_targets_from_routing` pushes it, unless a failover path pushes sooner.

### 3.2 `GET /routing`

- Returns each logical row **plus** **`source_sidecar_*`** / **`target_sidecar_*`** from a **join** on `devices` (assignment logic is unchanged).
- **Who should use this:** operators, dashboard, tests, or tools that want “edge X is routed to server Y” **and** the registered QUIC endpoints in one payload.
- **Who should not depend on it for data plane:** the **server** does not need routing rows or source-sidecar fields to **reply** to traffic (see §10).

### 3.3 Health monitor (`health_monitor.py`, `_routing_tick`)

After startup grace, every **`check_interval`**: failover / `assign_unrouted_edges` (same as before); then periodically **`_sync_stream_targets_from_routing`**, which **`PUT`s** each edge daemon using **`devices.sidecar_*`** for the **target** server.

## 4. `devices`: canonical QUIC endpoint per daemon

Columns: **`sidecar_host`**, **`sidecar_quic_port`**.

- Set at **`POST /register`** from the deployment daemon (or any client).
- **`deployments.sidecar_name`** is still returned by `/deploy` for Docker cleanup; **stream-target pushes** use **`devices`** only (`get_device_sidecar`).

## 5. Resolving `routing.target_device` → edge `/stream-target`

**Health monitor** (`_stream_target_peer`):

- **`get_device_sidecar(cluster, target_server)`** → `(host, quic_port)` from **`devices`**.
- **`PUT /stream-target`** body: **`{ "target_ip": <host>, "quic_port": <int> }`** (host may be an IP string or DNS name).

## 6. Edge daemon `/stream-target` and the edge sidecar

### 6.1 Daemon API (`control-plane/DeploymentDaemon/main.py`)

- **`PUT /stream-target`** accepts **`target_ip`** and **`quic_port`**; persists **`stream-target.json`** with the same keys.
- **`GET /stream-target`** returns those fields (or nulls if unset).

### 6.2 Edge sidecar (`sidecar/internal/edge/edge.go`)

- Polls **`GET {DaemonURL}/stream-target`** every **15s** (and once immediately at startup).
- Builds peer **`host:port`** from **`target_ip`** and **`quic_port`** (or env **`PEER_QUIC_PORT`** if `quic_port` is zero/absent).

## 7. End-to-end sequence (happy path)

1. Each daemon **registers** with daemon **HTTP** address + **sidecar** host/port.
2. **`routing`** rows assign edges to **server `device_id`s** (unchanged algorithm).
3. Health monitor **sync** resolves the **target server’s** `devices.sidecar_*` and **PUTs** the edge daemon.
4. Edge sidecar polls and dials QUIC.

## 8. Summary diagram

```
devices: (device_id) -> sidecar_host, sidecar_quic_port   [set at POST /register]

routing: (edge_id) -> (server device_id)                 [unchanged]

GET /routing: routing rows + join -> source_sidecar_*, target_sidecar_*

_push stream-target:
  (host, port) = devices row for target server
  PUT edge daemon /stream-target { target_ip: host, quic_port: port }
```

## 9. Remaining friction

1. **Registration does not push stream targets** — up to **30s** until periodic sync (unless failover).
2. **`assign_unrouted_edges` return value** is unused for immediate stream-target **PUT** after new routes (only failover orphan list triggers immediate push).
3. **`docs/routing.md`** may still describe older deployment-based resolution; treat **this file** and code as current.

## 10. Server replies: same connection / same local port — not `GET /routing`

**Inter-sidecar QUIC:** The **edge** dials the **server** sidecar (after `stream-target` provides the server’s QUIC host/port). Once the QUIC connection exists, the **server** sends frames back on that **same** connection and addressing — there is no separate “look up each edge’s `sidecar_host` from the controller” step for ordinary replies.

**Local sidecar ↔ inference app:** Today the app/sidecar UDP wiring can be asymmetric (separate send vs receive ports); a planned simplification is a **single local UDP port** where the sidecar **replies to the source address it last saw** (`recvfrom`), so the app is not selecting a per-peer target per packet either. See [SidecarUnifiedPortRefactor.md](./SidecarUnifiedPortRefactor.md).

**Registry role for servers:** The server still **`POST /register`s** with **`sidecar_host`** / **`sidecar_quic_port`** so the **cluster** knows where **edges** should dial and so **`GET /routing`** can show a consistent picture. That is **discovery and ops**, not the server’s reply routing table.

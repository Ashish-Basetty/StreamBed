# Plan: Per-device Docker networks + host-port publishing

## Context

Today every StreamBed container — controller, router, all 5 daemons, all daemon-spawned
sidecars, and all daemon-spawned inference containers — sits on a single shared bridge
network `streambed-net` ([docker-compose.yml:177-179](../docker-compose.yml#L177-L179)).
Containers find each other by name via Docker's embedded DNS:

- Daemon → controller via `http://controller:8080` (compose env)
- Daemon-spawned sidecar/inference are attached to whatever network the daemon
  itself is on, via [`_get_network()`](../shared/utils.py#L19-L25) which reads
  `/etc/hostname` to look up the daemon's own networks
  ([main.py:258, 303-307](../control-plane/DeploymentDaemon/main.py#L258),
  [sidecar_supervisor.py:78-80](../control-plane/DeploymentDaemon/sidecar_supervisor.py#L78-L80))
- Inference resolves its sidecar via `SIDECAR_HOST=<sidecar container name>` env var
  ([main.py:295-296](../control-plane/DeploymentDaemon/main.py#L295-L296))
- Cross-device (edge sidecar → server sidecar) uses the server sidecar's *container name*
  as `target_ip` in stream-target.json, returned by
  [`_stream_target_host()`](../control-plane/ControllerNode/streambed_controller/health_monitor.py#L315-L325)

This is fine for a local demo but it leaks the abstraction: every container can
talk to every other one by name, there is no isolation between "devices," and
the address scheme won't survive the multi-VM topology we already have stood
up in [infra/gcp/](../infra/gcp/) (1 VPC, 1 controller VM with a reserved
internal IP, 2 server VMs + 2 edge VMs, internal-only, subnet `10.10.0.0/24`).
The capstone story needs **logical devices** that look the same whether they
are co-tenants on one Docker host or each their own GCP VM.

**Goal:** each logical device = its own Docker bridge network containing
`{daemon, sidecar, inference}`. Cross-device traffic goes over **published host
ports** addressed as `host_ip:host_port`. The code path is identical
single-host and multi-host — the only thing that changes is the value of
`DAEMON_PUBLIC_IP` (and therefore the IP each daemon registers with the
controller). No shared backbone network, no cross-device Docker DNS.

## Why single-host and multi-host are the same code path

| | Single-host (laptop) | Multi-host (GCP) |
| --- | --- | --- |
| `DAEMON_PUBLIC_IP` | `host.docker.internal` (or `172.17.0.1` on Linux CI) | The VM's subnet internal IP (e.g. `10.10.0.5`), from the GCP metadata server |
| What dials it | Other daemons/sidecars on the same Docker host | Other VMs on the same VPC |
| Reachability | Loopback via the host's gateway | VPC firewall — internal subnet allows all TCP/UDP ([infra/gcp/main.tf:30-39](../infra/gcp/main.tf#L30-L39)) |
| Sidecar publish | `-p <host_udp>:<container_udp>/udp` on the device bridge | Same |

The plumbing inside the daemon (pick a free port, spawn sidecar with `-p`,
register `host_ip:host_port` with controller) is identical. Only the value
of `DAEMON_PUBLIC_IP` and `CONTROLLER_URL` differ, and those are env vars.

## Target topology

```
┌── streambed-device-edge1-net (bridge) ──┐  ┌── streambed-device-server1-net ──┐
│  daemon-edge1                            │  │  daemon-server1                  │
│  streambed-default-edge-001-sidecar      │  │  streambed-default-server-001-…  │
│  streambed-default-edge-001-<hash>       │  │  streambed-default-server-001-…  │
└──────────────────────────────────────────┘  └──────────────────────────────────┘
       │ (publishes UDP <bind> → host:<picked>)   │ (publishes UDP <bind> → host:<picked>)
       └────────────── HOST ──────────────────────┘
                            │
            streambed-control-net (bridge, controller + router only)
            controller :8080 (host-published)
            router     :8090 (host-published)
```

Controller and router sit on their own bridge `streambed-control-net` (separate
from any device). Daemons reach the controller **only** via the
`CONTROLLER_URL` env var — never via cross-network Docker DNS. In local dev
that's `http://host.docker.internal:8080`. In prod the env var points at
Caddy (TLS termination) which fronts the controller; on GCP today it points
straight at the controller's reserved internal IP per
[infra/gcp/vms.tf:30](../infra/gcp/vms.tf#L30). This makes the controller's
location a config concern, not a topology concern.

## Dynamic port allocation, no race

The daemon owns its host. It picks a free UDP port from a configurable range
*before* spawning the sidecar, so the address is known by the time `/deploy`
returns and there is no `docker inspect` race:

```
SIDECAR_PORT_RANGE_MIN=7000
SIDECAR_PORT_RANGE_MAX=7999
```

Sequence on `/deploy`:
1. Daemon picks a free UDP port in the range (bind probe; release; remember).
2. Persist the picked port to daemon state.
3. Spawn sidecar with `ports={f"{SIDECAR_QUIC_BIND_PORT}/udp": picked_port}`.
4. Return `{sidecar_host_ip, sidecar_host_port=picked_port}` in `/deploy`
   response — so the controller can push the first stream-target immediately.

The 1:1 inference↔sidecar invariant (one sidecar per inference container,
co-located on the same device net, lifetime-tied) is already true today; the
plan codifies it as an explicit assertion in `spawn_sidecar`.

## Sidecar address propagation (implemented)

Heartbeats today come from the **inference container**, not the daemon
([shared/heartbeat.py:18](../shared/heartbeat.py#L18)), so the original idea
of "daemon heartbeat carries the sidecar endpoint" did not fit cleanly. We
dropped heartbeat-carried propagation for v1 and rely on three triggers
instead:

1. **`/deploy` response** carries `sidecar_host_ip` + `sidecar_host_port`.
   `record_deployment()` writes both to the `deployments` row.
2. **Immediate push from `/deploy`** for servers: after recording the
   deployment, the controller's `_push_target_to_routed_edges()` PUTs
   `/stream-target` on every edge currently routed to this server. Catches
   the case where the edges registered first and the server's deployment
   landed after.
3. **Push on new route assignment**: the routing tick's
   `assign_unrouted_edges()` return value drives a `_push_targets_for_edges`
   call so an edge that gets routed *after* the server deployed also receives
   its target without waiting for the 30s bulk sync.

Daemon state (`deployed.json`) persists the picked sidecar host port so
daemon restarts reuse the same port, keeping the stored endpoint stable.
Heartbeat-carried updates can be added later if sidecar port churn becomes a
real concern.

## Critical files to modify

| File | Change |
| --- | --- |
| [docker-compose.yml](../docker-compose.yml) | Split one network per daemon (`streambed-device-edge{1,2,3}-net`, `streambed-device-server{1,2}-net`) + `streambed-control-net` for controller/router. Add `extra_hosts: ["host.docker.internal:host-gateway"]` to every daemon. Set `CONTROLLER_URL=http://host.docker.internal:8080` (local dev) — overridable. Drop `DAEMON_ADDRESS=daemon-edgeN`; add `DAEMON_PUBLIC_IP` (defaulted to `host.docker.internal`) and `SIDECAR_PORT_RANGE_{MIN,MAX}`. No hand-assigned sidecar ports. |
| [control-plane/DeploymentDaemon/daemon_config.py](../control-plane/DeploymentDaemon/daemon_config.py) | Add `DAEMON_PUBLIC_IP`, `SIDECAR_PORT_RANGE_MIN`, `SIDECAR_PORT_RANGE_MAX`. Keep `SIDECAR_QUIC_BIND_PORT` (the in-container bind). Remove `DAEMON_ADDRESS` once references are gone. |
| [control-plane/DeploymentDaemon/main.py](../control-plane/DeploymentDaemon/main.py) | Pick free UDP port in the configured range before spawning sidecar; persist to state. `/deploy` response gains `sidecar_host_ip` + `sidecar_host_port`. Register with controller using `DAEMON_PUBLIC_IP`. Lifespan: ensure-exists the device network (`networks.create(..., check_duplicate=True)`). Heartbeat (existing) payload extended with current sidecar endpoint. |
| [control-plane/DeploymentDaemon/sidecar_supervisor.py](../control-plane/DeploymentDaemon/sidecar_supervisor.py) | `spawn_sidecar` accepts `host_udp_port` arg; sets `ports={f"{QUIC_BIND}/udp": host_udp_port}`. Network attach unchanged (`_get_network()` → device net). Assert 1:1 with inference container (refuse to spawn if one already exists for the device). |
| [shared/utils.py](../shared/utils.py) | `_get_network()` works as-is — daemon is on exactly one network. |
| [control-plane/ControllerNode/streambed_controller/db.py](../control-plane/ControllerNode/streambed_controller/db.py) | Extend `deployments` row with `sidecar_host_ip TEXT NOT NULL`, `sidecar_host_port INTEGER NOT NULL`. Per [feedback_explicit_fields](../../.claude/projects/-Users-ashish-Code-CS214-StreamBed/memory/feedback_explicit_fields.md) — required, never inferred. |
| [control-plane/ControllerNode/streambed_controller/deploy.py](../control-plane/ControllerNode/streambed_controller/deploy.py) | Capture `sidecar_host_ip` / `sidecar_host_port` from daemon `/deploy` response; write to deployment row. |
| [control-plane/ControllerNode/streambed_controller/health_monitor.py](../control-plane/ControllerNode/streambed_controller/health_monitor.py) | `_stream_target_host()` returns `(sidecar_host_ip, sidecar_host_port)` from the deployments table. Drop the container-name path. On heartbeat with changed sidecar endpoint, re-push the new target to every edge currently routed at that server. If a deployment is missing the endpoint, return *not-ready* (no stale fallback to `get_device_ip`). |
| [control-plane/ControllerNode/streambed_controller/main.py](../control-plane/ControllerNode/streambed_controller/main.py) | Heartbeat endpoint accepts and validates the new sidecar-endpoint payload. |
| [tests/test_advisor_smoke.py:152-158](../tests/test_advisor_smoke.py#L152-L158) | Remove `_set_stream_target` — let the controller push the target after `deploy_advisor.sh`. The test's hardcoded `_SERVER_SIDECAR` container-name push becomes invalid under host-port addressing. |
| [docker-compose.throttle.yml](../docker-compose.throttle.yml) | Attach throttle proxy to `streambed-device-server1-net` so it stays an in-network MITM that resolves `server-001` via in-device DNS. |
| [experiments/advisor/docker-compose.advisor.yml](../experiments/advisor/docker-compose.advisor.yml) | Build-only services; verify no runtime network references. |

## Drop / cleanup

Concrete dead code/config to remove as part of this PR (no scope creep beyond
what the migration leaves dead):

- The server-side DNS alias at [main.py:306](../control-plane/DeploymentDaemon/main.py#L306)
  (`connect_kw["aliases"] = [DEVICE_ID]`) — existed so other things on the
  shared net could resolve `server-001`. With host-port addressing it has no
  purpose.
- `DAEMON_ADDRESS=daemon-edgeN` env in compose + the corresponding
  `DAEMON_ADDRESS` config field in `daemon_config.py`.
- `_stream_target_host`'s fallback to `get_device_ip` — superseded by the
  explicit deployment-row endpoint. Replace with a "not-ready" return path.

**Env-var audit during execution**: while editing, grep across `daemon_config.py`,
`docker-compose*.yml`, `experiments/`, and `shared/` for any other env vars
that become dead (e.g. unused `STREAMBED_*` paths if there are any). List
them in the PR description; remove only the ones the migration *makes* dead.
Don't expand scope into unrelated cleanup.

## Challenges with the IP-based transition

These are the rough edges to plan around — not blockers, just things that bite
if we ignore them:

1. **Container IPs are ephemeral.** Anything that captures an internal bridge
   IP will go stale on restart. Counter: never store internal IPs — always
   `host_ip:host_port`, stable because the daemon controls the port mapping
   and pre-allocates the host port.
2. **Docker DNS does not span networks.** Sidecar ↔ inference still resolves
   by name (same device net, fine). Cross-device must be host-port. Anything
   that currently relies on cross-device DNS — chiefly `_stream_target_host`
   and the throttle proxy — has to be rewritten. **Rule for this PR**: no
   Docker name resolution is allowed across device networks. If a code path
   tries it, that's a bug.
3. **`host.docker.internal` on Linux** requires `extra_hosts:
   ["host.docker.internal:host-gateway"]` on every container that needs to
   reach the host. macOS Docker Desktop has it built in; Linux CI runners do
   not. Add the directive uniformly. (Not relevant on GCP VMs, where there
   is no "host.docker.internal" — `DAEMON_PUBLIC_IP` becomes the VM's
   internal IP from the metadata server.)
4. **Port allocation.** Sidecar host UDP port is picked dynamically from
   `[SIDECAR_PORT_RANGE_MIN, MAX]` (default `7000-7999`). Daemon is
   authoritative — controller never assigns ports. Local dev: all daemons
   share one host's port space, range gives plenty of headroom. GCP: each
   daemon is on its own VM, no contention at all. Identical code.
5. **Sidecar registration race**: eliminated by pre-allocating the port
   before spawning the sidecar. The endpoint is known the moment `/deploy`
   returns. Heartbeat keeps it fresh thereafter.
6. **`device_ip` semantics change.** Today `devices.ip` is a container name
   on `streambed-net`. After this migration it's a host-routable address
   (`host.docker.internal` locally; VM internal IP on GCP). **Existing rows
   in `controller.db` from prior runs become invalid — user will manually
   `rm control-plane/data/controller.db` before first run.** (Test fixtures
   already do this via `_remove_controller_db`.)
7. **Throttle proxy semantics**: kept in-network by attaching to
   `streambed-device-server1-net` and continuing to resolve `server-001` via
   in-device DNS. Routing it through `host_ip:host_port` would change it from
   a transparent in-network proxy to a host-level interceptor — different
   blast radius, not the migration we want here.

## Verification

End-to-end, in order:

1. `docker compose up -d` — all services come up; `docker network ls` shows
   `streambed-device-{edge1,edge2,edge3,server1,server2}-net` and
   `streambed-control-net`.
2. `docker network inspect streambed-device-edge1-net` — only `daemon-edge1`
   attached pre-deploy; sidecar + inference appear post-`/deploy`.
3. `pytest tests/test_advisor_smoke.py` — existing assertions hold:
   `"QUIC connected"` in edge sidecar logs, sidecar metrics non-zero,
   `total_advised > 0`, no `"no such host"`. The implicit cross-device hop
   is now host-port — that's what we want.
4. Negative isolation test: `docker exec streambed-daemon-edge1 getent hosts streambed-daemon-server1`
   → **must fail** (cross-device DNS is gone).
5. Positive in-device DNS test: `docker exec streambed-default-edge-001-sidecar getent hosts streambed-default-edge-001-<hash>`
   → **must succeed**.
6. Heartbeat propagation: stop & re-spawn a server sidecar (forcing a
   different picked port), confirm the edge's `/stream-target` reflects the
   new port within one heartbeat interval.
7. Run `tests/throughput/` if applicable — QUIC throughput unchanged (one
   extra kernel netfilter pass on the host hop; expect <1% delta on
   loopback).

## Out of scope (future)

- **Multiplexed single server sidecar (one QUIC instance, label-routed across
  N edges).** Powerful but a separate architecture: server publishes one port,
  inference container learns per-frame routing labels, edges don't need to
  publish anything. Pros: one cert/port/process. Cons: single failure domain,
  shared backpressure, server-side horizontal scaling needs a sharding story.
  Worth a follow-on plan after this isolation lands.
- **Inference↔sidecar transport (UDP vs TCP).** Today it's TCP. The
  cross-device hop is QUIC-over-UDP for a reason (HOL-blocking-free
  multiplexing); converting that to TCP loses the property. Inference↔sidecar
  could plausibly stay TCP or move to UDS. Separate plan if revisited.
- **Caddy + TLS in front of controller** ([NginxControllerWrap.md](NginxControllerWrap.md))
  + DNS resolution for `CONTROLLER_URL` in prod. The plan keeps
  `CONTROLLER_URL` as the abstraction boundary so this is a config change
  later, not a code change.
- **Dynamic device registration** where the controller assigns devices to
  daemons / port pools on demand — current compose still hand-declares 5
  devices.
- **GCP startup-script wiring** to bring up the new compose on each VM
  ([infra/gcp/startup.sh](../infra/gcp/startup.sh) currently just installs
  Docker).

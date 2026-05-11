# QUIC Dynamic Routing Plan

## Goal

Allow the controller to reassign an edge sidecar to a different server at runtime — no container restart. The sidecar tears down the current QUIC connection and establishes a new one in-place when the target changes.

## Design

**Source of truth for peer address: daemon's `/stream-target` endpoint.**
`PEER_ADDRESS` / `--peer` is removed entirely. The sidecar polls `GET http://<DAEMON_URL>/stream-target` every 15 s in a background goroutine. On startup it blocks until the endpoint returns a valid `target_ip`. When `target_ip` changes, it tears down the current QUIC connection and redials — sidecar container never restarts.

No volume mount, no host path complexity, no filesystem I/O in the sidecar. The daemon already persists `stream-target.json` to disk, so idempotent restarts are handled transparently: daemon restarts → file reloaded → endpoint serves same value → sidecar reconnects to same peer.

**Polling in a separate goroutine.**
A goroutine inside `edge.Run()` polls the daemon endpoint every 15 s. When it detects a new `target_ip` it sends on a local `rerouteCh := make(chan string, 1)` — never exposed in `Config`. Blocking is minimal; the main pump loop is unaffected.

**No daemon-side changes.**
The daemon already has `GET /stream-target` and persists via `stream-target.json`. Nothing new needed on the daemon side.

---

## Changes

### 1. `sidecar/internal/edge/edge.go`

- Remove `PeerAddr string` from `Config`.
- Add `DaemonURL string` — base URL of the co-located daemon (e.g. `http://streambed-daemon-edge1:9090`).
- Add `PeerQUICPort int` — QUIC port to connect on (default 4433). Peer addr is `target_ip:PeerQUICPort`.
- Inside `edge.Run()`:
  - Local `rerouteCh := make(chan string, 1)`.
  - Local goroutine polls `DaemonURL/stream-target` every 15 s (JSON `{"target_ip": "..."`}); sends new peer addr on `rerouteCh` when `target_ip` changes. Blocks at startup until first valid `target_ip` received.
  - Outer reconnect loop: select on `ctx.Done`, `rerouteCh`, or pump-error.
  - On signal: cancel `connCtx`, drain `errc`, close conn, update `peerAddr`, redial.
  - `sentRate.Run(ctx)` starts once before the loop (sidecar lifetime, not per-connection).

### 2. `sidecar/cmd/streambed-quic-sidecar/main.go`

- Remove `peer` flag and `PEER_ADDRESS` env var.
- Add `daemonURL` flag: `env("DAEMON_URL", "")`.
- Add `peerQUICPort` flag: `env("PEER_QUIC_PORT", "4433")`.
- Fatal if `daemonURL == ""` for edge role.
- Metrics mux unchanged.

### 3. `control-plane/DeploymentDaemon/sidecar_supervisor.py`

- Remove `peer_address` parameter from `spawn_sidecar` (and its `PEER_ADDRESS` env entry).
- Add `daemon_url: str` parameter; pass as `DAEMON_URL` env var to the sidecar.
- Add `peer_quic_port: int` parameter; pass as `PEER_QUIC_PORT` env var.

### 4. `control-plane/DeploymentDaemon/main.py`

- Remove `SIDECAR_PEER_ADDRESS` usage in `_spawn_sidecar_for_role()`.
- Pass `daemon_url=f"http://streambed-{DEVICE_CLUSTER}-{DEVICE_ID}-daemon:{DAEMON_PORT}"` and `peer_quic_port=SIDECAR_QUIC_BIND_PORT` to `spawn_sidecar`.

  Wait — the daemon knows its own address as `DAEMON_ADDRESS`. The sidecar needs to reach the daemon over Docker's network. The daemon's container name on `streambed-net` is `streambed-daemon-{device_id}` (set by docker-compose). Pass `daemon_url` explicitly to avoid inferring the container name inside the sidecar code.

### 5. `control-plane/DeploymentDaemon/daemon_config.py`

- Remove `SIDECAR_PEER_ADDRESS`.

### 6. `docker-compose.yml`

- Remove `SIDECAR_PEER_ADDRESS` from all daemon service env blocks.

### 7. `tests/quic/conftest.py`

- Remove `PEER_ADDRESS` from edge env in `sidecar_pair_factory`.
- Start a minimal HTTP server per edge that serves `{"target_ip": "127.0.0.1", "target_port": 0}` at `/stream-target`.
- Pass `DAEMON_URL=http://127.0.0.1:<mock_port>` and `PEER_QUIC_PORT=<server_quic>` in edge env.

---

## Sequence (QUIC mode)

```
[startup]
  sidecar polls GET http://streambed-daemon-edge1:9090/stream-target
  daemon returns {"target_ip": "X", ...}
  sidecar dials X:4433 → QUIC connected

[reroute]
  controller → PUT /stream-target (daemon-edge1) with new target_ip=Y
  daemon updates stream-target.json, serves Y from GET endpoint
  sidecar poll goroutine (15 s) detects target_ip changed X→Y
  sends Y:4433 on rerouteCh
  reconnect loop: cancel connCtx → drain errc → close conn → dial Y:4433
  (sidecar container never restarts)

[daemon restart / idempotent]
  daemon restarts → loads stream-target.json → serves same target_ip
  sidecar reconnects to same peer (or new one if file was updated)
```

---

## Implementation notes

- Drain both `errc` slots after cancelling `connCtx` before starting new pump goroutines (avoids two concurrent pumps on the same UDP socket).
- Log clearly when polling: "edge: waiting for daemon target at {url}" on startup, "edge: rerouting → {peer}" on change.

---

## Exit criterion

- `test_controller_rerouting.py` updated to test QUIC path (new test in `tests/quic/`).
- `PEER_ADDRESS` / `--peer` gone from sidecar, daemon, and compose.
- No TCP proxy paths removed yet — follow-up after routing is verified.

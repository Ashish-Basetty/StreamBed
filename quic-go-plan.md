# Go QUIC Sidecar — Implementation Plan

## Context

StreamBed currently moves video frames from edge to server over **UDP** with a hand-rolled chunking protocol (`CHNK` magic, 8KB chunks — see [shared/stream_chunks.py](shared/stream_chunks.py#L7-L20)) and a JSON-encoded back-channel for bandwidth feedback (`{"received_bps": X}` — [server/app.py:124-139](server/app.py#L124-L139)). The daemon's `StreamProxyManager` handles fan-out, frame dropping, and rate adaptation in-process ([controller/DeploymentDaemon/stream_proxy_manager.py](controller/DeploymentDaemon/stream_proxy_manager.py)).

**Why change it.** Bare UDP over a network we do not control gives us no encryption, no congestion control, no path-MTU discovery, no connection identity, and no native demultiplexing of the planned ACTN/RATE control packets vs video data. We want to move to QUIC for the encryption alone; the rest is tailwind.

**Constraint.** The daemon ↔ inference container interface (TCP on the edge side, direct UDP listen on the server container) **does not change** in this revision. The sidecar replaces only the inter-host transport.

**Outcome.** A single Go binary (`streambed-quic-sidecar`) deployed *by the daemon itself* (same Docker-socket pattern the daemon already uses for inference containers — [controller/DeploymentDaemon/main.py:247](controller/DeploymentDaemon/main.py#L247)), running QUIC between edge-side and server-side hosts. Adaptive interleaving ([stream_proxy_manager.py:65-73](controller/DeploymentDaemon/stream_proxy_manager.py#L65-L73)) keeps working unmodified — the sidecar is a transport in this PR.

**Future-facing design note.** The transport layer (this PR) and the rate-adaptation policy layer (current daemon) are clean to compose now, but a follow-up PR may want to **fold the policy into the sidecar** to remove a hop and shave pipeline latency. The Go module structure below keeps that door open: rate logic lives behind a `Policy` interface in Go (initially a no-op pass-through) so it can be swapped from "Python-driven" to "sidecar-native" without rewriting the transport.

---

## Scope

**In scope**
- Replace the UDP wire between edge-side host and server-side host with QUIC ([quic-go](https://github.com/quic-go/quic-go)).
- Same binary, role chosen by env/flag (`SIDECAR_ROLE=edge|server`); roles live in cleanly separated packages.
- Daemon-managed lifecycle: daemon spawns and supervises the sidecar container on its own startup/registration.
- Preserve existing chunk format (drop-in for the daemon and the server container — they keep speaking the same payload bytes).
- Preserve the bandwidth-feedback channel (server → edge `received_bps` JSON).

**Not in scope (future PRs)**
- Replacing edge-container ↔ daemon TCP framing.
- Migrating to ACTN/RATE magic-prefix packet types (still raw JSON for feedback in this PR).
- Folding rate-adaptation policy into the sidecar.
- DINOv2 / model upgrade.
- Production PKI / mTLS / cert rotation.

---

## Architecture Options

| # | Approach | Where it sits | Pros | Cons |
|---|----------|---------------|------|------|
| **A** | **Datagram bump-in-the-wire**: Daemon writes UDP to `127.0.0.1:9050`, sidecar reads UDP, wraps each datagram into a QUIC **datagram** ([RFC 9221](https://datatracker.ietf.org/doc/html/rfc9221)) addressed to peer sidecar. Server-side sidecar unwraps and re-emits UDP. | Outside daemon and inference container; zero Python changes. | • Smallest blast radius — no Python refactor.<br>• Sidecar is stateless beyond the QUIC connection.<br>• Easy A/B with raw-UDP fallback.<br>• No protocol parsing in Go (sidecar treats payloads as opaque). | • All packet types share one unreliable channel — no separate handling for the eventual ACTN/RATE control plane.<br>• If a control-plane reliability requirement appears, this needs a second migration.<br>• MTU: QUIC datagrams cannot fragment. Current `CHUNK_SIZE=8000` ([stream_chunks.py:8](shared/stream_chunks.py#L8)) far exceeds path MTU once QUIC framing (~30B) and IP/UDP headers are deducted. Chunk size must drop to ~1200. |
| **B** | **Stream-multiplexing sidecar**: Sidecar parses the 4-byte magic prefix on each datagram (`CHNK`/`RATE`/`ACTN`) and routes by type — video chunks over QUIC datagrams (unreliable, low latency), control over a reliable QUIC stream. | Outside daemon, but parses wire format. | • Right tool per packet type: video drops cheaply, control is reliable.<br>• Native head-of-line avoidance between video and control flows.<br>• Sets the stage for the planned ACTN/RATE protocol without a second migration. | • Sidecar must understand the wire protocol → small parser duplication (Go + Python).<br>• Marginally more state (open streams per peer).<br>• One more failure mode in tests (stream resets).<br>• Same MTU requirement as A — video is still on datagrams, so `CHUNK_SIZE` must drop to ~1200. |
| **C** | **Semantic gRPC-style sidecar (FUTURE PR — see end of doc)**: Daemon stops writing UDP. Sidecar exposes a Unix-socket gRPC API (`SendFrame`, `SendFeedback`). Sidecar owns framing, chunking, QUIC streams, and eventually the policy layer. | Replaces UDP path *inside* daemon process boundary. | • Cleanest abstraction; transport + policy both hidden behind RPC.<br>• Lets us delete `make_chunks` and `_UDPSendOnlyProtocol` eventually.<br>• Natural home for the future merge of sidecar + adaptive interleaving. | • Largest Python diff: new RPC client in `StreamProxyManager`, generated stubs, schema.<br>• Two parallel codepaths during cutover.<br>• Too big for a single PR. |
| **D** | **QUIC datagrams only, no streams** (variant of A, no control plane): Same as A but explicitly punting on reliability forever. | Outside daemon. | • Minimal Go code, minimal failure modes.<br>• Encryption + congestion control + connection ID — already a win over raw UDP. | • No path forward for RATE/ACTN multiplexing.<br>• Doesn't justify the Go dependency on its own. |

---

## Recommended Approach: **Option B (stream-multiplexing sidecar)**

**Why B over A.** Both A and B require the chunk-size drop (video rides QUIC datagrams in either case, and datagrams cannot fragment). MTU is therefore not a differentiator. The decisive factor is the control plane: B gives RATE/ACTN a reliable, head-of-line-isolated channel from day 1; A throws that away and forces a second migration when control-plane reliability is needed. Cost of B over A: one 4-byte magic-prefix dispatch in Go. Worth it.

**Why B over C.** C is the right end-state, but the Python-side refactor (replacing `_UDPSendOnlyProtocol`, `transport.sendto`, the feedback-callback wiring) doubles the surface area of this change and delays shipping. Land B first, prove QUIC works in production, then lift to C in a focused follow-up.

### Architecture (Option B)

```
[edge container] --TCP--> [edge daemon] --UDP localhost--> [edge sidecar]
                                                                  |
                                                          QUIC connection
                                                          ├─ datagram channel: CHNK packets (unreliable, low-latency)
                                                          └─ stream "control":  RATE / ACTN packets (reliable, ordered)
                                                                  |
[server container] <--UDP localhost-- [server sidecar] <----------+
```

The sidecar is **transport-only** in this PR. It does not parse `StreamFrame` structure — only the 4-byte magic at the head of each datagram, and routes accordingly.

### Go module layout

```
sidecar/
  cmd/streambed-quic-sidecar/
    main.go                     # flag/env parsing, role dispatch
  internal/
    common/                     # shared types, magic constants, metrics
    edge/                       # edge-role: read local UDP, push to peer
    server/                     # server-role: receive QUIC, emit local UDP
    quictransport/              # quic-go wrappers, connection lifecycle, datagram + stream helpers
    policy/                     # Policy interface (no-op today; future home for adaptive logic)
    metrics/                    # prom-style counters + periodic log line
```

`edge/` and `server/` never import each other. Both depend on `quictransport/` and `common/`. `policy.Policy` is `interface { OnEgress(payload []byte) []byte }` returning the payload to send (or nil to drop) — initially a passthrough; future-PR's adaptive logic plugs in here without touching transport code.

---

## Implementation Steps

### Step 1 — Sidecar binary (Go) ✅
Go module at [sidecar/](sidecar/) with packages: `cmd/streambed-quic-sidecar`, `internal/common`, `internal/edge`, `internal/server`, `internal/quictransport`, `internal/policy`, `internal/metrics`. Uses [quic-go v0.48.2](sidecar/go.mod). Container image: `golang:1.22-alpine` build → `scratch` runtime.

### Step 2 — Daemon-managed sidecar lifecycle ✅
Implemented in [controller/DeploymentDaemon/sidecar_supervisor.py](controller/DeploymentDaemon/sidecar_supervisor.py). Pattern mirrors the inference-container lifecycle:
- `spawn_sidecar(...)` removes any stale container, then calls `client.containers.run(image, ...)` with env vars `SIDECAR_ROLE`, `PEER_ADDRESS`, `LOCAL_UDP_BIND`, `DAEMON_ADDRESS`, `QUIC_BIND`, `LOCAL_SERVER_UDP`, `DEVICE_ID`, `DEVICE_CLUSTER`.
- Container name: `streambed-{cluster}-{device_id}-sidecar` — resolvable via Docker DNS.
- `kill_sidecar(...)` does best-effort `container.kill()` + `container.remove(force=True)`.
- Daemon calls `_spawn_sidecar_for_role()` immediately after registration; calls `kill_sidecar` before deregister — coupled lifecycle, no split-brain ([controller/DeploymentDaemon/main.py](controller/DeploymentDaemon/main.py)).

**`device_type` note.** `deploy_to_device` now requires `device_type: str` (see [deploy.py](controller/ControllerNode/deploy.py)). The sidecar spawns via `docker-py` directly through `sidecar_supervisor`, bypassing `deploy_to_device` entirely — sidecar lifecycle is daemon-private and never touches the controller DB.

### Step 3 — Wire the daemon into the sidecar ✅
`STREAM_TRANSPORT=quic` in [stream_proxy_manager.py](controller/DeploymentDaemon/stream_proxy_manager.py) redirects `transport.sendto` to `(127.0.0.1, SIDECAR_LOCAL_UDP_PORT)` instead of the peer directly. Default `udp` until soak passes. Compose env vars wired for all five daemons in [docker-compose.yml](docker-compose.yml): `STREAM_TRANSPORT`, `SIDECAR_IMAGE`, `SIDECAR_PEER_ADDRESS` (edge daemons point to server-001 sidecar by DNS name).

### Step 4 — Chunk-size drop ✅
`CHUNK_SIZE` dropped `8000` → `1200` in [shared/stream_chunks.py](shared/stream_chunks.py). The `todo: change to <1400` comment at [shared/interfaces/stream_interface.py](shared/interfaces/stream_interface.py) is resolved. Unit assertions added in [tests/unit/test_stream_chunks.py](tests/unit/test_stream_chunks.py): `test_chunk_size_is_quic_safe` (≤1300), `test_large_payload_reassembles_byte_for_byte` (N≥10 chunks, byte-for-byte identity).

### Step 5 — TLS bootstrap (throwaway, dev-only) ✅
`DevTLSConfig` in [sidecar/internal/quictransport/devcert.go](sidecar/internal/quictransport/devcert.go) generates a self-signed ECDSA P-256 cert in-process at startup (1-year validity, SAN includes both `localhost` and the peer name). `InsecureSkipVerify: !isServer` — clients skip verification in dev, servers verify nothing. The `TODO(prod-pki)` comment references `QuicSidecar.md "TLS bootstrap"` as the grep target for the production migration.

### Step 6 — Metrics + logs ✅
[sidecar/internal/metrics/metrics.go](sidecar/internal/metrics/metrics.go): `Registry` with atomic counters (`datagrams_sent/received`, `datagram_bytes_sent/received`, `stream_bytes_sent/received`, `handshake_ms`, `rtt_ms`). `ServeHTTP` serves Prometheus text at `:9100/metrics` (main.go wires it); `LogLoop` emits the same snapshot every 10s at INFO. One gap: `RTTNanos` is never populated — `PollRTT()` in `transport.go` is a stub (`_ = stats` no-op). quic-go exposes RTT via internal connection stats that aren't exported in a stable API as of v0.48; either scrape `quic.Connection.Stats().SmoothedRTT` (requires a version upgrade or reflection) or keep the counter at 0 and note it in `QuicSidecar.md`.

### Step 7 — Cutover & rollback ✅
`STREAM_TRANSPORT=quic|udp` flag wired into [daemon_config.py](controller/DeploymentDaemon/daemon_config.py) (default `udp`). Compose comment in [docker-compose.yml](docker-compose.yml) documents the one-env-var rollback procedure. `QuicSidecar.md` at project root still to be written.

### Step 8 — Couple sidecar lifecycle to inference container lifecycle
**Move sidecar spawn/kill from daemon startup/shutdown to `/deploy` and `/delete`.** Today the sidecar boots with the daemon (in `lifespan`), which means a sidecar exists even before any inference container does. The user-facing semantic should be: a sidecar exists *if and only if* an inference container is deployed on this device.

**Changes ([controller/DeploymentDaemon/main.py](controller/DeploymentDaemon/main.py)):**
- Remove `_spawn_sidecar_for_role()` call from `lifespan` startup; remove `kill_sidecar` from shutdown.
- In `/deploy`, after `client.containers.run(...)` succeeds, call `_spawn_sidecar_for_role()`. The sidecar is **standardized** — its config (role, peer address, ports) comes entirely from daemon env vars, no per-deployment customization. `spawn_sidecar` is idempotent (force-removes any prior sidecar before re-running), so repeated `/deploy` calls produce a fresh sidecar each time. No deployment-state record needed.
- In `/delete`, before iterating inference containers, call `kill_sidecar(...)`. Update the `streambed-{cluster}-{device}-` prefix filter to **exclude** the `-sidecar` container so the iteration deletes inference containers only — sidecar is killed explicitly via `kill_sidecar` for clarity.
- Daemon shutdown (`lifespan` exit) still calls `_deregister_with_retries()` but no longer touches the sidecar. If the daemon dies with an inference container still running, the sidecar is orphaned but harmless — the next `/deploy` force-removes it.

**Why no deployment-state recording:** the sidecar is not parameterized by deployment image / port / hash. Recording it in `deployment_state.json` would be redundant — `spawn_sidecar` already infers everything from daemon env. Keep the deployment record focused on the inference container.

**Edge case:** redeploying an inference container does a full sidecar restart (brief streaming gap, ≈ handshake_ms ≤ 100ms). Acceptable for v1.

---

## Current Streaming Test Weaknesses

(From audit of [tests/test_integration_stream_to_storage.py](tests/test_integration_stream_to_storage.py), [tests/test_dynamic_interleaving.py](tests/test_dynamic_interleaving.py), [tests/unit/test_stream_interface.py](tests/unit/test_stream_interface.py), [tests/unit/test_network_simulation.py](tests/unit/test_network_simulation.py), [tests/unit/test_bandwidth_estimator.py](tests/unit/test_bandwidth_estimator.py), [tests/throttle_proxy/proxy.py](tests/throttle_proxy/proxy.py), [tests/throughput/](tests/throughput/).)

| Weakness | Where it bites | Why it matters for QUIC |
|---|---|---|
| **Loopback only.** Every streaming test sends to `127.0.0.1`. No real RTT, no real loss. | `test_stream_interface.py:21-54`, `test_network_simulation.py:71-120`, `test_integration_stream_to_storage.py:51-128` | QUIC's value (congestion control, reliability) is invisible at zero RTT. Bugs in retransmission timers, 0-RTT, version negotiation never surface. |
| **Throttle proxy throttles, does not drop.** | `tests/throttle_proxy/proxy.py:22-62` | Token-bucket delays packets but never loses them. The interleaving controller's interaction with real loss is untested. |
| **Throughput harness has no assertions.** | [tests/throughput/run_throughput.py:179-244](tests/throughput/run_throughput.py#L179-L244) | Injects 50ms delay and 10% loss but only *prints* metrics. Useless as a CI gate. |
| ~~**Tests reach into private state.**~~ **Fixed ✅** `receiver._queue` / `receiver._transport` replaced with `recv_one(timeout)` and `get_local_port()` public API in all three test files ([test_integration_stream_to_storage.py](tests/test_integration_stream_to_storage.py), [test_network_simulation.py](tests/unit/test_network_simulation.py), [test_stream_interface.py](tests/unit/test_stream_interface.py)). | All transport tests | A QUIC-backed receiver will not have these attributes. Tests will break in transit. |
| **Feedback loop is not actually closed in any integration test.** Comment at `test_dynamic_interleaving.py:94-95` admits the proxy doesn't forward feedback. | test_dynamic_interleaving.py | Verifying that adaptive-rate-under-loss works requires the back channel to actually flow. Today, no test does this. |
| **No long-soak.** `THROTTLE_RUN_SEC = 15` ([test_dynamic_interleaving.py:30](tests/test_dynamic_interleaving.py#L30)). | test_dynamic_interleaving.py | QUIC behaviors that emerge over minutes (idle timeout, congestion-window growth, mem leaks) are invisible. |
| **No multi-flow contention.** Each test runs one edge → one server. | All | Real deployment has 3 edges → 2 servers. Per-connection congestion-control fairness is untested. |
| **No MTU edge cases.** Frames are 16×16×3 — never close to any MTU. | `make_stream_frame` in test_integration_stream_to_storage.py:16-26 | Hides chunk-reassembly bugs that the chunk-size drop will exercise. |
| **Bitwise equality only on synthetic frames.** | test_integration_stream_to_storage.py:106 | Real JPEGs / larger embeddings could mask serialization bugs. |

---

## Reliability Test Plan

Five new test layers, tagged `integration_quic`, runnable via `pytest -m integration_quic`.

### Layer 1 — Property-based codec round-trip ✅
[tests/quic/test_codec_property.py](tests/quic/test_codec_property.py): Hypothesis `@given(payload=st.binary(..., max_size=CHUNK_SIZE*20))` — any payload round-trips byte-for-byte through `make_chunks` + reassembly. Also covers the Go dispatch table (`go test ./sidecar/internal/common/...` via `protocol_test.go`). Both are full implementations.

### Layer 2 — Fault-injection harness (`chaosproxy`) ✅ / ⬜
[tests/quic/chaosproxy.py](tests/quic/chaosproxy.py): full Gilbert-Elliott loss + jitter + duplication + reorder-window implementation. Usable in-process or as a script.

[tests/quic/test_chaos_matrix.py](tests/quic/test_chaos_matrix.py): scaffold with three parametrized scenarios (`clean`, `light_loss`, `burst_loss`). **The test body needs to be implemented** — it imports `ChaosProxy` and creates frames, but doesn't actually drive send/receive through the proxy or assert delivery ratios. The integration requires deciding whether to drive through the Python `StreamBedUDPSender/Receiver` pair (no Go binary needed) or through actual sidecar containers.

### Layer 3 — Closed-loop adaptive-rate test ✅ (partial)
[tests/quic/test_adaptive_under_loss.py](tests/quic/test_adaptive_under_loss.py): `test_bandwidth_estimator_responds_to_feedback` smoke-tests the `CompositeBackend`/`ServerFeedbackBackend` API — passes without the sidecar. The full closed-loop test (drive frames through chaosproxy, assert `should_drop_video_frame` engages) is deferred to `test_dynamic_interleaving.py` once the feedback path is wired; see weakness table.

### Layer 4 — Long-soak + leak detection ⬜
[tests/quic/test_soak.py](tests/quic/test_soak.py): scaffold only. Skipped unless `STREAMBED_RUN_SOAK=1`. Needs: RSS scraper for `:9100/metrics`, 15-minute chaosproxy run, slope-of-RSS assertion. Tagged `pytest -m soak`.

### Layer 5 — Multi-flow contention ⬜
[tests/quic/test_multiflow_fairness.py](tests/quic/test_multiflow_fairness.py): scaffold only. Skipped unless `go` is on PATH. Requires: 3 `StreamBedUDPSender` goroutines (or one saturating sender + two lighter ones), one sidecar pair, fairness assertion ≥30% for non-saturating flows. Tagged `pytest -m soak`.

### Decoupling existing tests from private state ✅
`recv_one(timeout)`, `get_local_port()`, and `queue_size()` added to `StreamBedUDPReceiver` ([shared/interfaces/stream_interface.py](shared/interfaces/stream_interface.py)). All three test files refactored off `receiver._queue` / `receiver._transport`.

---

## Critical Files To Be Modified

| File | Change | Status |
|---|---|---|
| `controller/DeploymentDaemon/main.py` | Spawn sidecar container post-registration; kill on shutdown. | ✅ |
| `controller/DeploymentDaemon/sidecar_supervisor.py` | `spawn_sidecar` / `kill_sidecar` — daemon-private lifecycle. | ✅ New |
| `controller/DeploymentDaemon/daemon_config.py` | `STREAM_TRANSPORT`, `SIDECAR_IMAGE`, `SIDECAR_PEER_ADDRESS`, `SIDECAR_LOCAL_UDP_PORT`, `SIDECAR_QUIC_BIND_PORT`, `SIDECAR_FEEDBACK_PORT`. | ✅ |
| `controller/DeploymentDaemon/stream_proxy_manager.py` | Route `forward_frame` to local sidecar UDP under `STREAM_TRANSPORT=quic`. | ✅ |
| `controller/DeploymentDaemon/tcp_utils.py` | Source of UDP feedback packets becomes the local sidecar; no functional change. | ⬜ |
| `shared/stream_chunks.py` | `CHUNK_SIZE = 8000` → `1200`. | ✅ |
| `shared/interfaces/stream_interface.py` | `recv_one()`, `get_local_port()`, `queue_size()` public API; `todo` comment removed. | ✅ |
| `docker-compose.yml` | `STREAM_TRANSPORT`, `SIDECAR_IMAGE`, `SIDECAR_PEER_ADDRESS` env vars on all five daemons. | ✅ |
| `server/app.py` / `server/server_config.py` | `STREAM_LISTEN_HOST` already env-configurable (default `0.0.0.0`). Set to `127.0.0.1` in compose when `STREAM_TRANSPORT=quic`. Compose env var not yet added. | ⬜ |
| `tests/throughput/proxy.py` | `chaosproxy.py` written as a superset; original not removed. | ✅ (new written) |
| `tests/test_dynamic_interleaving.py:94-95` | Wire feedback through chaosproxy. | ⬜ |
| `tests/test_integration_stream_to_storage.py`, `tests/unit/test_network_simulation.py`, `tests/unit/test_stream_interface.py` | Replaced private-state access with `recv_one()` / `get_local_port()`. | ✅ |
| `tests/unit/test_stream_chunks.py` | Chunk-size and reassembly assertions. | ✅ New |
| `sidecar/` Go module | Full Option B: edge, server, quictransport, policy, metrics, devcert, main. | ✅ New |
| `sidecar/Dockerfile` | Multi-stage `golang:1.22-alpine` → `scratch`, ~10MB. Exposes `4433/udp 9100/tcp`. | ✅ New |
| `tests/quic/chaosproxy.py` | Full Gilbert-Elliott + jitter + dup + reorder implementation. | ✅ New |
| `tests/quic/test_codec_property.py` | Hypothesis round-trip (Python). Go dispatch covered by `protocol_test.go`. | ✅ New |
| `tests/quic/test_adaptive_under_loss.py` | Bandwidth estimator smoke. Full closed-loop deferred. | ✅ (partial) |
| `tests/quic/test_chaos_matrix.py` | Scaffold: scenarios defined, test body not implemented. | ⬜ Scaffold |
| `tests/quic/test_soak.py` | Scaffold: skipped unless `STREAMBED_RUN_SOAK=1`. | ⬜ Scaffold |
| `tests/quic/test_multiflow_fairness.py` | Scaffold: skipped unless `go` on PATH. | ⬜ Scaffold |
| `QuicSidecar.md` | Operator runbook: env vars, rollback, RTT stub note, TLS migration path. | ⬜ |
| `SemanticSidecarFuture.md` | Option C future-PR sketch. | ⬜ |

---

## Future PR — Option C: Semantic gRPC-Style Sidecar

Captured here for now; promote to its own `SemanticSidecarFuture.md` at the project root when this PR lands. Goal: collapse transport, chunking, and rate-adaptation policy into the sidecar so the daemon's `StreamProxyManager` shrinks to a thin gRPC client and the per-frame Python overhead disappears.

**Sketch:**
- Sidecar exposes a Unix-socket gRPC service: `SendFrame(stream Frame) → ()`, `Subscribe() → stream Feedback`.
- `Frame` proto carries `payload bytes`, `is_video bool`, `frame_interleaving_rate float`.
- Sidecar implements `should_drop_video_frame` natively in Go — the `Policy` interface from this PR's module layout becomes load-bearing.
- Daemon retains `BandwidthEstimator` until a follow-up moves it Go-side too.
- Wire format on the QUIC side stays as in Option B (so this is an internal Python ↔ Go boundary change, not a wire-protocol change). 0-downtime cutover possible if both sidecar versions are deployed during transition.

**Files that will move (eventually):**
- `controller/DeploymentDaemon/stream_proxy_manager.py` — most of it deleted; replaced by `stream_proxy_client.py` (~50 lines of gRPC).
- `shared/stream_chunks.py`, `shared/bandwidth/*` — usage shrinks; eventual port to Go is its own ticket.
- `controller/DeploymentDaemon/tcp_utils.py` — `_UDPSendOnlyProtocol` deleted entirely.

**Why split it out:** doubling the Python diff on top of a Go-rewrite is too much for one PR. Prove QUIC works first.

---

## Verification

End-to-end, in order:

1. **Unit (Python):** `pytest tests/unit/` — passes today. Covers chunk-size, `recv_one()`, bandwidth estimator, network simulation. ✅ Runnable now.
2. **Unit (Go):** `cd sidecar && go test ./...` — covers magic-prefix dispatch in `protocol_test.go`. ✅ Runnable now (requires Go toolchain).
3. **Property:** `pytest tests/quic/test_codec_property.py` — Hypothesis round-trip (requires `hypothesis`). ✅ Runnable now.
4. **Adaptive smoke:** `pytest tests/quic/test_adaptive_under_loss.py` — bandwidth estimator + feedback convergence. ✅ Runnable now.
5. **Integration smoke:** `STREAM_TRANSPORT=quic pytest tests/test_integration_stream_to_storage.py` — same assertions, new transport. ⬜ Blocked on `server/app.py` listen-host flip and sidecar binary in path.
6. **Chaos matrix:** `pytest tests/quic/test_chaos_matrix.py -v`. ⬜ Test body not implemented.
7. **Multi-flow fairness:** `pytest tests/quic/test_multiflow_fairness.py`. ⬜ Scaffold.
8. **Soak (manual / weekly):** `STREAMBED_RUN_SOAK=1 pytest -m soak tests/quic/test_soak.py`. ⬜ Scaffold.
9. **Manual:** `docker compose up`; `curl http://localhost:9100/metrics` on each sidecar; check daemon logs for `[Daemon] sidecar spawned` log line; sanity-check `handshake_ms` is sub-100ms. ⬜ Requires sidecar image push.
10. **Rollback:** `STREAM_TRANSPORT=udp docker compose up`; rerun step 1 — must still pass on the original UDP path. ✅ Runnable now.

A green run of steps 1–7 plus a clean step 10 is the bar to land Option B in `main`.

**Remaining blockers before full integration smoke:**
- `test_chaos_matrix.py` body (Layer 2) — drive `ChaosProxy` with `StreamBedUDPSender/Receiver` and assert delivery ratio.
- `server/app.py` compose env: add `STREAM_LISTEN_HOST=127.0.0.1` when `STREAM_TRANSPORT=quic` in `docker-compose.yml`.
- RTT counter in `transport.go`: `PollRTT` is a stub; populate `m.RTTNanos` from `q.Stats().SmoothedRTT` (quic-go v0.48 exposes this as `quic.Connection.Stats()`).
- `QuicSidecar.md`: document env vars table, rollback steps, RTT stub workaround, and `TODO(prod-pki)` reference.

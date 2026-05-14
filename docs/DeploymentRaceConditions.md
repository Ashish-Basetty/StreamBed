# Deployment race conditions

Catalogued from the advisor Phase D smoke walkthrough on 2026-05-13. Each entry
names a real race or schema mismatch in the controller → daemon → sidecar →
inference path. Ranked by smoke-flakiness likelihood, not severity.

The smoke test in question:
`tests/test_advisor_smoke.py::test_advisor_phase_d_docker_smoke_one_episode`.
Recurring symptom: `frame_gen` raises `asyncio.IncompleteReadError: 0 bytes
read on a total of 32 expected bytes` after the first frame.

## Quick reference

| # | Where | Effect | Fix priority |
|---|---|---|---|
| D | Server advisor's handshake datagram dropped if server sidecar UDP not bound yet | `lastInfer` unset → server→app QUIC payloads dropped silently; advice never flows | High |
| K | Edge inference handshake dropped if edge sidecar UDP not bound yet | `lastApp` unset → server→edge CSTR dropped silently; advice never reaches edge | High |
| I | `EdgeInference.predict` raises on first frame → `writer.close()` with zero bytes written | **Matches IncompleteReadError symptom exactly.** TCP peer gets EOF mid-header. | High |
| F | 15s edge-sidecar `pollDaemonTarget` interval between stream-target PUT and dial | Adds up to 15 s to startup; currently within the 45 s `_wait_for_log` budget but eats most of it | Medium |
| H | `frame_gen` connects before edge inference's TCP listener is up | `ConnectionRefused` — different failure mode, but same root cause class | Medium |
| C | Python `create_datagram_endpoint` resolves `SIDECAR_HOST` at startup | If Docker DNS hasn't caught up, `serve()` exits, container dies, frame_gen sees connection refused | Medium |
| G | Edge sidecar QUIC dial retry loop while server sidecar still binding | 2 s retry; within budget | Low |
| E | Test PUTs `target_port`, edge sidecar decodes `quic_port` | Mismatch silently masked by `PEER_QUIC_PORT=4433` env default coincidentally matching `SIDECAR_QUIC_BIND_PORT` | Medium (latent bug, will bite on port reconfig) |
| B | Daemon `/register` payload omits `sidecar_host` / `sidecar_quic_port` | `devices.sidecar_*` never populated → health-monitor sync writes wrong IPs (daemon IP, not sidecar) | High (correctness — but currently masked by tests setting stream-target manually) |
| A | `_wait_for_daemons` returns on `/health=200` | Not actually a race — FastAPI lifespan blocks /health until `_register_with_retries` completes — but worth verifying after any framework upgrade | Note only |

---

## Details

### D / K — UDP handshake dropped against an unbound sidecar

**Where:**
- `experiments/advisor/server/advisor_server.py` `serve()` — `await duplex.connect(SIDECAR_HOST, SIDECAR_UDP_PORT)`
- `experiments/advisor/edge/edge_inference.py` `serve()` — same call
- `shared/interfaces/stream_interface.py` `StreamBedUDPDuplex.connect` — sends one JSON handshake datagram

**Timeline (server side):**
1. Daemon `/deploy` starts the inference container (line 308 of `control-plane/DeploymentDaemon/main.py`).
2. Inference container's Python imports torch (~3–5 s) then calls `duplex.connect`.
3. Meanwhile daemon spawns the sidecar (line 311), which `ListenUDP` ~100 ms after start.

Usually the sidecar is bound before the Python handshake fires. When it isn't:
- Kernel drops the datagram silently.
- Sidecar's `lastInfer` / `lastApp` stays `nil`.
- Every QUIC→UDP payload (`pumpDatagramsToUDP`, `pumpControlToUDP`, `pumpControlIntoBandwidth`) gets the `if dst == nil { continue }` branch.
- The advisor server's `receive_stream()` never yields; edge never receives CSTR advice.

**Suggested fix:**
Have `StreamBedUDPDuplex.connect` send periodic handshakes until it observes
inbound traffic (or a separate priming task). Alternatively, have the sidecar
emit its own "ready" datagram outward once `Run` enters its main loop — but
the sidecar doesn't know the app's address yet, so the app must be the
initiator. Periodic handshake is cleaner.

### I — `predict()` raises on first frame, TCP peer gets EOF

**Where:** `experiments/advisor/edge/edge_inference.py` `EdgeInference.predict`, lines around the tuple unwrap / `feat.reshape(...)`.

The diff added two defensive branches:
```python
raw_feat = self.teacher.policy.extract_features(x)
if isinstance(raw_feat, tuple):
    feat = raw_feat[0]
if feat.dim() > 2:
    feat = feat.reshape(feat.size(0), -1)
```

These were added because *something* about the SB3 feature-extractor output
broke. The branches are guesses, not validations. If the actual shape doesn't
match `SmallHead`'s input dim, `self.head(feat)` raises, and the
`handle_connection` outer `try/finally` closes the TCP writer with **zero
bytes written** — exactly the `IncompleteReadError(0/32)` symptom.

**Suggested fix:**
- Assert expected dims at load time (`hidden`, `in_dim` are in `head_meta`).
- On predict failure, log the traceback **and** write a sentinel ACTN to the
  host rather than closing silently. The host already has a `(int) action`
  field; sentinel-error code lets the framing layer recover.

### F — Edge sidecar 15s poll for stream-target

**Where:** `sidecar/internal/edge/edge.go` `pollDaemonTarget`.

`check()` fires immediately on startup, then a `time.NewTicker(15*time.Second)`
takes over. So worst case is ~15 s between test's PUT and edge sidecar's next
fetch. The smoke's `_wait_for_log(..., timeout=45)` tolerates this, but it
eats one-third of the budget.

**Suggested fix:**
- Daemon writes to a shared file already — have the sidecar `fsnotify`-watch
  it instead of polling. Or
- Daemon pings the sidecar over UDP (one-byte trigger) on PUT, sidecar polls
  on receipt — half-keep current model, half-event-driven.

The doc (`RoutingTableAndSidecars.md` §3.1) already calls this out as a "gap"
with a 30 s health-monitor sync as the alternative push path.

### H — `frame_gen` connects before edge inference's TCP listener up

**Where:** `tests/test_advisor_smoke.py` `_wait_for_log(_EDGE_SIDECAR, "QUIC connected")` waits for the **sidecar**, not the **inference** process.

Edge inference's TCP `start_server` happens after torch + checkpoint loads
(~5 s). If the sidecar achieves "QUIC connected" first (it can, on hot Docker
DNS), frame_gen starts immediately and may hit ECONNREFUSED.

**Suggested fix:** add a `_wait_for_log(edge_container, "edge_inference TCP listening")`
between the QUIC-connected wait and `frame_gen` invocation. Cheap, deterministic.

### C — Python `create_datagram_endpoint` DNS at startup

**Where:** `shared/interfaces/stream_interface.py` `StreamBedUDPDuplex.connect`
calls `loop.create_datagram_endpoint(..., remote_addr=(host, port))` —
resolves `host` once, at startup.

If Docker DNS for `SIDECAR_HOST` hasn't propagated, this raises (`getaddrinfo`
failure), the `serve()` coroutine exits, container exits, frame_gen sees
ECONNREFUSED.

**Suggested fix:** retry `create_datagram_endpoint` with backoff up to e.g.
30 s. Or pre-resolve with `socket.getaddrinfo` in a retry loop, then pass the
IP. Cleanest: retry in the duplex's own connect().

### G — Edge sidecar QUIC dial retry while server sidecar binds

**Where:** `sidecar/internal/edge/edge.go` `Dial(...)` failure → `time.After(2*time.Second)` retry.

Usually fine. Only matters under unusual cold-start latency. No fix needed.

### E — `target_port` vs `quic_port` schema mismatch

**Where:**
- Test: `tests/test_advisor_smoke.py` `_set_stream_target()` — body has `target_port`.
- Daemon: `control-plane/DeploymentDaemon/main.py` `put_stream_target` — accepts `target_port`, writes JSON with that key.
- Doc: `docs/RoutingTableAndSidecars.md` §5 — says the field is `quic_port`.
- Go sidecar: `sidecar/internal/edge/edge.go` `fetchTarget` — decodes `quic_port,omitempty`.

So the test writes `target_port: 9000`, the Go decoder sees `quic_port: 0`,
and falls back to `PEER_QUIC_PORT` env (`SIDECAR_QUIC_BIND_PORT=4433` from
the daemon's spawn call). 4433 is the server sidecar's QUIC port, so it
*happens* to work.

**Suggested fix:** pick one wire name, audit all three sites. `quic_port` is
the more accurate name and matches the doc — change daemon and test.

### B — Daemon `/register` payload omits sidecar host/port

**Where:** `control-plane/DeploymentDaemon/main.py` `_register_with_retries`
payload has `(device_cluster, device_id, device_type, ip, port)` — no
`sidecar_host`, no `sidecar_quic_port`.

`RoutingTableAndSidecars.md` §1 says these are required. Without them,
`devices.sidecar_host` / `sidecar_quic_port` are NULL, and the health-monitor
sync's `_stream_target_host` falls through to `get_device_ip` — i.e. it
pushes the **daemon IP** (host loopback) instead of the sidecar container
name. Edges then can't QUIC-dial.

In the advisor smoke this is masked because the test PUTs stream-target
manually with the correct sidecar name. In production deployment after a
device restart, the controller's first sync writes the wrong target.

**Suggested fix:** populate `sidecar_host` and `sidecar_quic_port` in the
register payload from `SIDECAR_REGISTER_HOST` env + `SIDECAR_QUIC_BIND_PORT`.
Daemon already knows both.

### A — `_wait_for_daemons` polls /health only

**Where:** `tests/deploy_utils.py` `_wait_for_daemons`.

Not actually a race — FastAPI lifespan startup completes before /health
serves 200, and `_register_with_retries` lives inside lifespan startup. So
registration is guaranteed done by the time /health responds. Worth verifying
after any FastAPI version bump (lifespan semantics have changed before).

---

## Cross-cutting observation

The system has **three independent priming requirements**, each silently
broken if mistimed:

1. App→sidecar UDP handshake (registers `lastInfer` / `lastApp`).
2. Daemon→sidecar stream-target PUT (registers QUIC peer addr).
3. Daemon→controller register (registers QUIC peer addr in `devices`).

Each one fails silently and produces a different downstream symptom. There is
no end-to-end "ready" signal that captures "the full path is wired". A
synthetic `/preflight` endpoint on the daemon that returns the state of all
three would let tests fail fast with a clear message instead of timing out
on `_wait_for_log` or `IncompleteReadError`.

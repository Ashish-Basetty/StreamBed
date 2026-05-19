# Plan: End-to-end tests for StreamBed's adaptive rate-drop loop

## Context

Today the adaptive-dropping path in StreamBed has solid unit coverage but no
test that exercises the full closed loop:

```
server.pumpFeedback (real bytes-recv → EWMA → FBCK frame)
   └─► QUIC control stream
        └─► edge.pumpControlIntoBandwidth → RemoteBackend.Update
              └─► bandwidth.Composite(sentRate, remoteRate)
                    └─► policy.RateLimit.OnEgress (drops CHNK over budget)
```

Existing tests prove each *piece* works in isolation
([sidecar/internal/policy/policy_test.go](sidecar/internal/policy/policy_test.go),
[sidecar/internal/bandwidth/bandwidth_test.go](sidecar/internal/bandwidth/bandwidth_test.go))
with hand-set estimator values, and
[tests/test_dynamic_interleaving.py:89-117](tests/test_dynamic_interleaving.py#L89-L117)
inserts a token-bucket proxy in the wire path — but the proxy is one-way
(see [tests/throttle_proxy/proxy.py:37-63](tests/throttle_proxy/proxy.py#L37-L63))
so FBCK never travels back to the edge. The "drops" seen there are the
proxy dropping bytes at the wire, **not** the edge sidecar reacting to feedback.

This plan adds two integration tests so that the loop is observed working as
a system, not just as parts:

1. **Go in-process integration test** — fine-grained, deterministic, asserts
   on shared `*metrics.Registry` counters. Covers all three branches
   (FBCK-driven, SentRate-driven, negative cases).
2. **Python end-to-end test** — drives real UDP traffic through the compiled
   sidecar binaries via the existing `sidecar_pair_factory` fixture and
   scrapes `/metrics`. Smoke-level confirmation that the wire-level system
   behaves correctly.

The user's stated preferences (recorded during planning):
- Assertion shape: **observed datagrams at the receiver** — no new
  `DatagramsDropped` metric, no test-only policy wrapper.
- FBCK control: **drive real traffic; let `pumpFeedback` measure it** — do
  not inject a fake `bytesRecv` closure.

The Composite estimator is `min(sentRate, remoteRate)`. Because the SentRate
sampler clamps to `[10_000, 50_000_000]` bps with hardcoded defaults
([sampling.go:36-65](sidecar/internal/bandwidth/sampling.go#L36-L65)) and
`edge.Run` constructs it inline ([edge.go:54](sidecar/internal/edge/edge.go#L54)),
we need a small injection knob on `edge.Config` so tests can force the budget
low enough that drops actually occur on a localhost link. See "Pre-work."

---

## Pre-work: tiny refactor to make the test feasible

### A. Make `edge.Run` accept estimator knobs

[sidecar/internal/edge/edge.go:31-43](sidecar/internal/edge/edge.go#L31-L43) — extend `Config`:

```go
type Config struct {
    // …existing fields…
    Policy        policy.Policy            // already exists
    SentRateCfg   bandwidth.SamplingConfig // NEW — zero value preserves defaults
    RemoteDefault uint64                   // NEW — defaults to 500_000 if zero
}
```

Then [edge.go:54-58](sidecar/internal/edge/edge.go#L54-L58):

```go
sentRate := bandwidth.NewSampling(cfg.Metrics.DatagramBytesSent.Load, cfg.SentRateCfg)
remoteDefault := cfg.RemoteDefault
if remoteDefault == 0 { remoteDefault = 500_000 }
remoteRate := bandwidth.NewRemote(remoteDefault)
```

Zero-value `SamplingConfig{}` preserves today's behaviour because
`NewSampling` already fills in defaults. **No production behaviour change.**

This is the smallest surface change that lets a Go test (a) cap the local
estimator low enough to force drops on loopback, and (b) let real traffic +
real `pumpFeedback` drive the FBCK side honestly.

### B. (Python side) Bidirectional, source-aware throttle proxy

[tests/throttle_proxy/proxy.py](tests/throttle_proxy/proxy.py) is one-way
(daemon → server only). For a Python end-to-end FBCK-loop test, the proxy
must learn the original source and forward server-originated packets
(including QUIC handshake ACKs and FBCK in the control stream) back. **The
existing dynamic-interleaving test relies on this proxy and is almost
certainly broken** — its `< 40` threshold is satisfied by `0` frames, which
happens if the QUIC handshake never completes through a one-way NAT.

Replace the single recv-loop with a per-source forwarding map:
- One bound socket facing the daemon side, one facing the server side.
- `daemon → proxy_in_sock → throttle → proxy_out_sock → server`
- `server → proxy_out_sock → throttle → proxy_in_sock → daemon`
- Source-address tracking on `proxy_in_sock` so reverse-direction packets
  are addressed back to the last sender.

This is reused by:
- The Python test below.
- A fix for `test_dynamic_interleaving.py` (out of scope here but called out).

---

## Test 1 — Go in-process integration test

**Location:** `sidecar/internal/integration/adaptive_drop_test.go` (new
package — `integration_test` directory keeps it out of the `edge` and
`server` package import cycles).

### Harness

A `startPair(t *testing.T)` helper that:

1. Allocates three ephemeral ports — server QUIC, server UDP, edge UDP — via
   `net.ListenUDP("udp", ":0")` then `.LocalAddr().(*net.UDPAddr).Port` and
   close-immediately.
2. Spawns an `httptest.NewServer` that responds to `GET /stream-target` with
   `{"target_ip":"127.0.0.1","target_port":<server_quic>}`. Mirrors what
   the Python `MockDaemonServer` does
   ([tests/quic/conftest.py:266](tests/quic/conftest.py#L266)) — verify the
   exact JSON shape by reading `edge.pollDaemonTarget` first.
3. Constructs `serverMetrics, edgeMetrics := metrics.New(), metrics.New()` and
   keeps both references for later assertions.
4. `go server.Run(ctx, server.Config{...Metrics: serverMetrics})`.
5. Polls `serverMetrics` or just sleeps 250 ms — easier path: dial QUIC
   directly until accept succeeds, or borrow the
   `wait_for_metrics` style by exposing `/metrics` via
   `metrics.Registry.ServeHTTP`.
6. `go edge.Run(ctx, edge.Config{ ..., SentRateCfg: <subtest-controlled> })`.
7. Returns `{edgeUDP int, serverMetrics, edgeMetrics, cancel func()}`.

Cleanup: `t.Cleanup(cancel)` plus wait-group on both `Run` goroutines so the
test doesn't leak between subtests.

### Subtest 1.1 — `TestAdaptiveDrop_SentRateBranch`

The easiest of the three because it doesn't need a slow link.

- `SentRateCfg = SamplingConfig{ Max: 50_000, Min: 10_000, SafetyFactor: 0.9, Alpha: 0.5, Interval: 200 * time.Millisecond }`
- Burst on `RateLimit` defaults to ~1 s of capacity at target bps, so ~5 KB.
- Drive **synthetic CHNK** UDP frames into `edgeUDP`. Build them as
  `TagCHNK[:] + payload`. Use payload size ~2 KB and rate ~1000/sec
  (offered ≈ 16 Mbps, way above the 50 kbps clamp).
- Run for `2 * SentRateCfg.Interval + smoothing-lag ≈ 1 s` then read
  `edgeMetrics.DatagramsSent` and `serverMetrics.DatagramsReceived`.
- **Assertions:**
  - `serverMetrics.DatagramsReceived < 0.5 * sent_by_test` (offered) —
    proves drops happened.
  - `edgeMetrics.DatagramsSent ≈ serverMetrics.DatagramsReceived ± slack` —
    proves drops happened **at the edge policy**, not on the network. Any
    edge-counted send corresponds to a write into QUIC, which on loopback
    should arrive.

### Subtest 1.2 — `TestAdaptiveDrop_FBCKBranch`

Tests the closed loop with **real `pumpFeedback`** measuring real bytes.

- Set `SentRateCfg = SamplingConfig{ Max: 50_000_000 }` (basically uncapped
  locally) so the local branch can't be the one that caused drops.
- `RemoteDefault = 50_000_000` — initial budget high so traffic flows.
- Drive synthetic CHNK at ~2 Mbps for `≥ 3 * feedbackInterval` = 6 s.
- Halfway through, **stop sending entirely** for 2.5 s (one feedback cycle
  ticks while no bytes flow → measured received rate plummets towards 0 →
  EWMA pulls FBCK down).
- Resume sending at 2 Mbps. Now: edge sees a low `RemoteBackend.TargetBps`
  but local sentRate clamp is 50 Mbps, so composite = remote = low → drops.
- **Assertions:**
  - During the "post-resume" window, `edgeMetrics.DatagramsSent` minus the
    pre-pause baseline is meaningfully smaller than offered — and matches
    `serverMetrics.DatagramsReceived` for that window.
  - Cross-check: poll the edge's exposed `/metrics` (via `Registry.ServeHTTP`
    on a `net/http/httptest.Server`) or expose the `RemoteBackend` for the
    test by stashing a pointer in `edge.Config`. Cleanest: add `OnReady
    func(*bandwidth.RemoteBackend)` callback to `edge.Config` so the test
    can observe the value directly. (Decide during implementation — only do
    this if metric-only assertions prove too flaky.)

### Subtest 1.3 — `TestAdaptiveDrop_ControlAndEMBDPassThrough`

Same harness as 1.1 (clamped sent-rate). Mixed traffic:
- CHNK at 1000/s × 2 KB (drops expected).
- EMBD at 10/s × 200 B (must all arrive).
- CSTR at 10/s × 200 B (must all arrive).
- Tag bytes from `sidecar/internal/common/protocol.go` constants.

Send each on its own UDP source port from the test so we can count by
classifying what arrives on the server-side unified UDP port — or simpler,
distinguish by inspecting the 4-byte tag of each packet the server's app
would have written back. Cleanest: route CHNK to one socket, EMBD/CSTR to
another, and assert per-stream loss rates.

**Assertions:**
- EMBD count received == EMBD count sent (zero loss).
- CSTR count received == CSTR count sent (zero loss).
- CHNK loss > 50%.

---

## Test 2 — Python end-to-end test

**Location:** `tests/quic/test_adaptive_drop.py` (new).

### Setup

Reuses [tests/quic/conftest.py:221-322](tests/quic/conftest.py#L221-L322)
`sidecar_pair_factory(edge_count=1, enable_reverse=True)`. The reverse path
priming is needed because we want the test to mimic an inference app — the
edge sidecar must know `lastApp` so reverse-direction control frames
(including future-CSTR replies) land somewhere; this also keeps parity with
real deployments.

The test sets the edge sidecar's sampling clamp via a **new env var**.
Plumb `STREAMBED_SENTRATE_MAX_BPS` (and friends) through to the `edge.Run`
config in [sidecar/cmd/sidecar/main.go](sidecar/cmd/sidecar/main.go) (read
files there to confirm exact path during implementation). Default behaviour
unchanged when unset.

### `test_sentrate_self_throttles_localhost`

- Set `STREAMBED_SENTRATE_MAX_BPS=50000`.
- Start pair via fixture.
- For 3 seconds, spam 2 KB CHNK datagrams at the edge UDP port from a tight
  Python loop — target ~5 Mbps offered.
- Scrape `http://127.0.0.1:<edge_metrics>/metrics` and
  `http://127.0.0.1:<server_metrics>/metrics`.
- **Assert:** `streambed_sidecar_datagrams_received` (server) is at least
  10× less than `streambed_sidecar_datagrams_sent` from the test loop. And:
  edge's `streambed_sidecar_datagrams_sent` is within ~10% of the server's
  `streambed_sidecar_datagrams_received` (drops are at the policy, not the
  wire).

### `test_fbck_loop_through_throttle_proxy` (gated on Pre-work B)

This subtest requires the bidirectional throttle proxy. If the proxy fix
ships in the same PR, include this; otherwise mark `pytest.mark.skip(reason="needs bidirectional throttle proxy")` and pair with a follow-up issue.

- Bring up controller + daemons + bidirectional throttle proxy
  (`docker-compose.yml` + the patched `docker-compose.throttle.yml`).
- Point edge daemon's stream-target at the proxy
  (`_put_stream_target(EDGE_DAEMON_URL, "throttle-proxy", 9010)`).
- Configure proxy `THROTTLE_RATE_BPS=50000`.
- Drive `mock_video_server` at high fps for ~20 s.
- Scrape both sidecars' `/metrics`. Verify:
  - Server `datagram_bytes_received` ≈ proxy rate × duration (proves the
    throttle is the binding constraint).
  - Edge `datagrams_sent` (CHNKs) is meaningfully below `mock_video_server`
    frame count × frames-per-CHNK (proves edge is dropping in response to
    FBCK, not just letting all CHNKs into QUIC and watching the proxy drop
    them).
  - The gap between offered (mock_video_server output) and
    edge.datagrams_sent is much wider than the gap between
    edge.datagrams_sent and server.datagrams_received — the wire isn't
    where drops are concentrated.

---

## Critical files

**Read-only / referenced:**
- [sidecar/internal/edge/edge.go](sidecar/internal/edge/edge.go) — edge.Run, pumpControlIntoBandwidth (L256-294)
- [sidecar/internal/server/server.go](sidecar/internal/server/server.go) — server.Run, handlePeer (L123-140)
- [sidecar/internal/server/feedback.go](sidecar/internal/server/feedback.go) — pumpFeedback
- [sidecar/internal/policy/policy.go](sidecar/internal/policy/policy.go) — RateLimit.OnEgress
- [sidecar/internal/bandwidth/sampling.go](sidecar/internal/bandwidth/sampling.go) — SamplingConfig knobs
- [sidecar/internal/bandwidth/remote.go](sidecar/internal/bandwidth/remote.go) — RemoteBackend
- [sidecar/internal/bandwidth/composite.go](sidecar/internal/bandwidth/composite.go)
- [sidecar/internal/common/protocol.go](sidecar/internal/common/protocol.go) — TagCHNK/EMBD/CSTR/CSTL/RATE/ACTN/FBCK constants
- [sidecar/internal/metrics/metrics.go](sidecar/internal/metrics/metrics.go) — Registry fields, ServeHTTP at L44
- [tests/quic/conftest.py](tests/quic/conftest.py) — sidecar_pair_factory at L221-322

**Modified:**
- `sidecar/internal/edge/edge.go` — add `SentRateCfg`, `RemoteDefault` to Config (Pre-work A).
- `sidecar/cmd/sidecar/main.go` — read `STREAMBED_SENTRATE_MAX_BPS` (+ siblings) env vars, wire into Config (Pre-work A's plumbing for the Python test).
- `tests/throttle_proxy/proxy.py` — make bidirectional with source-address tracking (Pre-work B).

**New:**
- `sidecar/internal/integration/adaptive_drop_test.go`
- `tests/quic/test_adaptive_drop.py`

---

## Verification

### Unit + integration (Go)

```bash
cd sidecar
go test ./internal/policy/... ./internal/bandwidth/... -v          # existing — must still pass
go test ./internal/integration/... -v -run TestAdaptiveDrop -count=3
```

`-count=3` because subtests assert on rate behaviour with timing windows;
running three times surfaces flakes.

### Python integration

```bash
# Sidecar binary must be built first; sidecar_binary fixture rebuilds.
pytest tests/quic/test_adaptive_drop.py -v
# Full end-to-end (gated on proxy refactor):
pytest -m integration_docker tests/quic/test_adaptive_drop.py::test_fbck_loop_through_throttle_proxy
```

### Regression checks

- `pytest tests/test_dynamic_interleaving.py` should still pass against the
  bidirectional proxy (and the threshold may need tightening — the old `<
  40` was almost meaningless; once the proxy actually works, frames should
  arrive at a known rate).
- `go test ./...` — full sidecar test suite.

### Manual confirmation

Spin up the pair under a debugger, drive the offered rate above the cap,
and watch the edge sidecar log line from `metrics.LogLoop`
([metrics.go:61-77](sidecar/internal/metrics/metrics.go#L61-L77)): the
`dg_sent` counter should grow more slowly than the test sender's offered
count.

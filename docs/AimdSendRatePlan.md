# AIMD Send-Rate Controller Plan

## Context

The edge sidecar's send-rate policy can only ratchet **down**. Today the policy target is `Composite.TargetBps()` = `min(SentRate, RemoteRate)` ([composite.go:14](../sidecar/internal/bandwidth/composite.go#L14)):

- `RemoteRate` is the last FBCK from the server ([remote.go:24](../sidecar/internal/bandwidth/remote.go#L24)). The server computes FBCK as `received_bps` it actually saw, EWMA-smoothed ([feedback.go:60](../sidecar/internal/server/feedback.go#L60)).
- `SentRate` is an EWMA of bytes the edge actually pushed ([sampling.go:97](../sidecar/internal/bandwidth/sampling.go#L97)).

When the link narrows, the server's received_bps drops → FBCK drops → edge throttles → edge sends less → server sees even less → FBCK stays low. `SentRate` mirrors the throttled send rate, so it locks in the floor from the other side. There is no path back up: both estimators are downstream of the policy's own throttling decision. This was the failure observed during yesterday's sweep.

**Goal:** turn the rate controller into an equilibrium system. Probe upward by a fixed additive step every feedback cycle, and only back off when there is actual evidence of loss (server received noticeably less than the edge sent in the same window). Classic AIMD.

## Approach

Replace `RemoteBackend` with `AIMDBackend`. FBCK arrival drives one AIMD step per cycle:

- **Additive increase**: if `received_bps ≥ 0.85 × sent_bps`, the link is keeping up → `target += Δ` (default Δ = 1 Mbps), clamped to max.
- **Multiplicative decrease**: if `received_bps < 0.85 × sent_bps` *and* `sent_bps` is above a floor (so we have a real measurement, not just idleness), `target = target × β` (default β = 0.7), clamped to min.
- **Idle guard**: if `sent_bps` is below ~min bound (we didn't push enough to learn anything), still additive-increase. We're not the bottleneck; let the bucket grow so the app can use the link when it has data.

The edge measures `sent_bps` over the FBCK window by sampling `Metrics.DatagramBytesSent` at FBCK arrival and diffing against the previous sample. No new instrumentation needed — the counter already exists ([metrics.go:18](../sidecar/internal/metrics/metrics.go#L18)).

Drop `SentRate` from the `Composite`. It's tautological under throttling and contributes to the lock-in. Keep the `SamplingBackend` instance running as a diagnostic gauge (still logged in the 1 s loop at [edge.go:85](../sidecar/internal/edge/edge.go#L85)), just not in the policy decision.

## Files to change

### New: `sidecar/internal/bandwidth/aimd.go`

```go
type AIMDBackend struct {
    sentBytes     func() uint64       // injected; uses Metrics.DatagramBytesSent.Load
    target        atomic.Uint64       // current TargetBps
    delta         uint64              // additive increase per cycle (bps)
    beta          float64             // multiplicative decrease factor (0..1)
    ratioThresh   float64             // congestion threshold: received/sent
    idleSentFloor uint64              // bps below which we treat the window as idle
    min, max      uint64              // clamp bounds
    initialBps    uint64

    mu        sync.Mutex              // OnFeedback is serial per connection
    prevBytes uint64
    prevTime  time.Time
    primed    bool
}

type AIMDConfig struct {
    Delta         uint64        // default 1_000_000
    Beta          float64       // default 0.7
    RatioThresh   float64       // default 0.85
    IdleSentFloor uint64        // default 50_000
    Min, Max      uint64        // defaults 10_000 / 50_000_000
    InitialBps    uint64        // default 20_000_000
}

func NewAIMD(sentBytes func() uint64, cfg AIMDConfig) *AIMDBackend { /* apply defaults */ }

func (b *AIMDBackend) TargetBps() uint64 { return b.target.Load() }

// OnFeedback runs one AIMD step. Caller passes the received_bps reported by
// the peer's FBCK frame.
func (b *AIMDBackend) OnFeedback(receivedBps uint64) {
    b.mu.Lock(); defer b.mu.Unlock()
    now := time.Now()
    cur := b.sentBytes()
    if !b.primed { b.prevBytes, b.prevTime, b.primed = cur, now, true; return }
    elapsed := now.Sub(b.prevTime).Seconds()
    delta := cur - b.prevBytes
    b.prevBytes, b.prevTime = cur, now
    if elapsed <= 0 { return }
    sentBps := uint64(float64(delta*8) / elapsed)

    t := b.target.Load()
    switch {
    case sentBps < b.idleSentFloor:
        t = b.additive(t)                                    // idle: still probe up
    case float64(receivedBps) < b.ratioThresh*float64(sentBps):
        t = b.multiplicative(t)                              // real loss detected
    default:
        t = b.additive(t)
    }
    b.target.Store(t)
}
```

Replaces `remote.go` (delete it after edge.go is migrated).

### `sidecar/internal/edge/edge.go`

- Replace `remoteRate := bandwidth.NewRemote(initialBps)` ([edge.go:64](../sidecar/internal/edge/edge.go#L64)) with `aimdRate := bandwidth.NewAIMD(cfg.Metrics.DatagramBytesSent.Load, bandwidth.AIMDConfig{InitialBps: initialBps})`.
- Build `Composite` with only `aimdRate` ([edge.go:65](../sidecar/internal/edge/edge.go#L65)). Keep `sentRate` constructed and `sentRate.Run(ctx)` going so the diagnostic log line at [edge.go:85](../sidecar/internal/edge/edge.go#L85) still shows it; just exclude it from the composite. (If Composite is now single-member, the wrapper is a no-op but harmless; leaves the seam for future estimators.)
- In `pumpControlIntoBandwidth` ([edge.go:287](../sidecar/internal/edge/edge.go#L287)), change `remote.Update(bps)` ([edge.go:316](../sidecar/internal/edge/edge.go#L316)) to `aimd.OnFeedback(bps)`. Update the parameter type and the log line to print the new target.

### `sidecar/internal/bandwidth/bandwidth_test.go`

Drop `TestRemote_*`. Add:

- `TestAIMD_AdditiveOnHealthyRatio` — feed equal sent/received over the window, target rises by Δ each step.
- `TestAIMD_MultiplicativeOnLossyRatio` — sent_bps high, received_bps = 0.5 × sent_bps → target falls by factor β.
- `TestAIMD_RecoversAfterBackoff` — drive a backoff, then resume healthy ratio, assert target climbs back past the post-backoff floor. **This is the regression test for the bug.**
- `TestAIMD_IdleStillProbesUp` — sent_bps below idleSentFloor → target still grows additively.
- `TestAIMD_ClampsToBounds` — confirm min/max enforced.

### `sidecar/internal/bandwidth/composite_test.go` (if it exists) and `policy_test.go`

No semantic change to either type; existing tests should pass unchanged.

## Out of scope

- Server-side `feedback.go` is unchanged. FBCK semantics ("received_bps over the last window") are preserved; the edge just interprets it differently.
- No new control-plane messages, no protocol bump.
- No RTT/loss plumbing from QUIC. If the ratio signal proves insufficient in experiments, that's a follow-up.

## Verification

1. `cd sidecar && go test ./internal/bandwidth/... ./internal/policy/... ./internal/edge/...` — unit tests, including the new `TestAIMD_RecoversAfterBackoff`.
2. `go build ./...` from `sidecar/` — confirm edge.go wiring compiles.
3. End-to-end experiment: run the existing harness with a stepped-burst schedule that drops capacity mid-run and restores it (see [tests/experiments/schedules/stepped_burst.yml](../tests/experiments/schedules/stepped_burst.yml)). Scrape `streambed_sidecar_target_bps` and confirm it (a) drops within ~one feedback cycle of the capacity cut and (b) climbs back to within ~20% of the pre-cut level after capacity is restored. The current code fails (b); the new code should pass.
4. Spot-check the edge log: `edge: bw sent_bps=… remote_bps=… composite_bps=…` should now show `remote_bps` (AIMD target) oscillating around the link rate, not monotonically decreasing.

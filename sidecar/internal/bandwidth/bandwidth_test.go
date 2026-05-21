package bandwidth

import (
	"context"
	"sync/atomic"
	"testing"
	"time"
)

// fixedEstimator is a tiny test-only Estimator used to exercise Composite
// without dragging in the dynamics of AIMD/Sampling.
type fixedEstimator uint64

func (f fixedEstimator) TargetBps() uint64 { return uint64(f) }

func TestComposite_PicksMin(t *testing.T) {
	composite := NewComposite(fixedEstimator(1_000_000), fixedEstimator(500_000), fixedEstimator(2_000_000))
	if got := composite.TargetBps(); got != 500_000 {
		t.Fatalf("TargetBps = %d, want 500_000", got)
	}
}

func TestComposite_EmptyFallback(t *testing.T) {
	composite := NewComposite()
	if got := composite.TargetBps(); got != 10_000 {
		t.Fatalf("TargetBps with no members = %d, want 10_000", got)
	}
}

func TestSampling_TracksObservedRate(t *testing.T) {
	var counter atomic.Uint64
	b := NewSampling(counter.Load, SamplingConfig{
		Interval:     20 * time.Millisecond,
		Alpha:        1.0, // no smoothing — observed value passes through directly
		SafetyFactor: 1.0, // no headroom for the test
		Min:          1_000,
		Max:          1_000_000_000,
		InitialBps:   1_000,
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = b.Run(ctx); close(done) }()

	// Push 10_000 bytes per 20ms → 4 Mbit/s.
	for i := 0; i < 10; i++ {
		counter.Add(10_000)
		time.Sleep(20 * time.Millisecond)
	}

	got := b.TargetBps()
	cancel()
	<-done

	// Allow plenty of slop — Go's ticker is not perfectly periodic.
	if got < 2_000_000 || got > 6_000_000 {
		t.Fatalf("TargetBps after sustained 4Mbit/s = %d, want ~4_000_000", got)
	}
}

func TestSampling_ClampsToBounds(t *testing.T) {
	var counter atomic.Uint64
	counter.Store(1_000_000_000) // baseline; deltas computed from this
	b := NewSampling(counter.Load, SamplingConfig{
		Interval:     10 * time.Millisecond,
		Alpha:        1.0,
		SafetyFactor: 1.0,
		Min:          1_000,
		Max:          5_000,
		InitialBps:   1_000,
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { _ = b.Run(ctx); close(done) }()

	// Way more than 5_000 bps would imply.
	for i := 0; i < 5; i++ {
		counter.Add(100_000)
		time.Sleep(10 * time.Millisecond)
	}

	got := b.TargetBps()
	cancel()
	<-done

	if got > 5_000 {
		t.Fatalf("TargetBps = %d, expected clamped to <= 5_000", got)
	}
}

// aimdHarness builds an AIMDBackend wired against an in-memory sent-bytes
// counter and a virtual clock the test controls directly. Each step advances
// the clock by 2 s (matching the FBCK cadence) and lets the test inject how
// many bytes were "sent" in that window before OnFeedback runs.
type aimdHarness struct {
	now     time.Time
	bytes   atomic.Uint64
	backend *AIMDBackend
}

func newAIMDHarness(cfg AIMDConfig) *aimdHarness {
	h := &aimdHarness{now: time.Unix(0, 0)}
	cfg.Now = func() time.Time { return h.now }
	h.backend = NewAIMD(h.bytes.Load, cfg)
	return h
}

// step advances the virtual clock by 2s, credits sentBytes to the counter,
// and feeds receivedBps through OnFeedback.
func (h *aimdHarness) step(sentBytes uint64, receivedBps uint64) {
	h.bytes.Add(sentBytes)
	h.now = h.now.Add(2 * time.Second)
	h.backend.OnFeedback(receivedBps)
}

func TestAIMD_PrimesOnFirstFeedback(t *testing.T) {
	h := newAIMDHarness(AIMDConfig{InitialBps: 10_000_000})
	h.step(0, 5_000_000) // first call only primes the window
	if got := h.backend.TargetBps(); got != 10_000_000 {
		t.Fatalf("after priming TargetBps = %d, want unchanged 10_000_000", got)
	}
}

func TestAIMD_AdditiveOnHealthyRatio(t *testing.T) {
	h := newAIMDHarness(AIMDConfig{InitialBps: 10_000_000, Delta: 1_000_000})
	h.step(0, 0) // prime
	// 250_000 bytes over 2s = 1 Mbit/s sent; received_bps matches → healthy.
	h.step(250_000, 1_000_000)
	if got := h.backend.TargetBps(); got != 11_000_000 {
		t.Fatalf("after one healthy step TargetBps = %d, want 11_000_000", got)
	}
	h.step(250_000, 1_000_000)
	if got := h.backend.TargetBps(); got != 12_000_000 {
		t.Fatalf("after two healthy steps TargetBps = %d, want 12_000_000", got)
	}
}

func TestAIMD_MultiplicativeOnLossyRatio(t *testing.T) {
	h := newAIMDHarness(AIMDConfig{
		InitialBps:    10_000_000,
		Beta:          0.5,
		RatioThresh:   0.85,
		IdleSentFloor: 50_000,
	})
	h.step(0, 0) // prime
	// 2.5 MB over 2s = 10 Mbit/s sent; received is 4 Mbit/s → ratio 0.4 < 0.85.
	h.step(2_500_000, 4_000_000)
	if got := h.backend.TargetBps(); got != 5_000_000 {
		t.Fatalf("after lossy step TargetBps = %d, want 5_000_000 (10M * 0.5)", got)
	}
}

// TestAIMD_RecoversAfterBackoff is the regression test for the lock-in bug
// the AIMD rework exists to fix. After a multiplicative drop, healthy
// feedback cycles must walk the target back up.
func TestAIMD_RecoversAfterBackoff(t *testing.T) {
	h := newAIMDHarness(AIMDConfig{
		InitialBps:  10_000_000,
		Delta:       1_000_000,
		Beta:        0.5,
		RatioThresh: 0.85,
	})
	h.step(0, 0) // prime

	// Backoff cycle: 10 Mbit/s sent, only 4 Mbit/s received.
	h.step(2_500_000, 4_000_000)
	postBackoff := h.backend.TargetBps()
	if postBackoff != 5_000_000 {
		t.Fatalf("post-backoff target = %d, want 5_000_000", postBackoff)
	}

	// Now the link heals: sent/received match in subsequent windows.
	for i := 0; i < 3; i++ {
		h.step(250_000, 1_000_000)
	}
	got := h.backend.TargetBps()
	if got <= postBackoff {
		t.Fatalf("after 3 healthy steps TargetBps = %d, expected > %d (post-backoff floor)",
			got, postBackoff)
	}
	if got != 8_000_000 {
		t.Fatalf("after 3 healthy steps TargetBps = %d, want 8_000_000 (5M + 3*1M)", got)
	}
}

func TestAIMD_IdleStillProbesUp(t *testing.T) {
	h := newAIMDHarness(AIMDConfig{
		InitialBps:    10_000_000,
		Delta:         1_000_000,
		IdleSentFloor: 50_000,
	})
	h.step(0, 0) // prime
	// Zero bytes sent in the window → idle path → additive increase, not backoff.
	h.step(0, 0)
	if got := h.backend.TargetBps(); got != 11_000_000 {
		t.Fatalf("idle TargetBps = %d, want 11_000_000 (additive even when received is 0)", got)
	}
}

func TestAIMD_ClampsToBounds(t *testing.T) {
	h := newAIMDHarness(AIMDConfig{
		InitialBps:  9_000_000,
		Delta:       1_000_000,
		Beta:        0.5,
		Min:         2_000_000,
		Max:         10_000_000,
		RatioThresh: 0.85,
	})
	h.step(0, 0) // prime
	// Push two healthy cycles: should hit Max and stick.
	h.step(250_000, 1_000_000)
	h.step(250_000, 1_000_000)
	if got := h.backend.TargetBps(); got != 10_000_000 {
		t.Fatalf("after additive past Max TargetBps = %d, want clamped 10_000_000", got)
	}
	// Crash the target with repeated backoffs; should floor at Min.
	for i := 0; i < 10; i++ {
		h.step(2_500_000, 100_000)
	}
	if got := h.backend.TargetBps(); got != 2_000_000 {
		t.Fatalf("after repeated multiplicative TargetBps = %d, want floored 2_000_000", got)
	}
}

func TestAIMD_ResetRestoresInitial(t *testing.T) {
	h := newAIMDHarness(AIMDConfig{
		InitialBps:  10_000_000,
		Delta:       1_000_000,
		Beta:        0.5,
		RatioThresh: 0.85,
	})
	h.step(0, 0)
	h.step(2_500_000, 4_000_000) // drive a backoff
	if got := h.backend.TargetBps(); got == 10_000_000 {
		t.Fatalf("expected target to move from 10_000_000 before Reset")
	}
	h.backend.Reset()
	if got := h.backend.TargetBps(); got != 10_000_000 {
		t.Fatalf("after Reset TargetBps = %d, want 10_000_000", got)
	}
	// First post-Reset call should only re-prime, not adjust.
	h.step(0, 0)
	if got := h.backend.TargetBps(); got != 10_000_000 {
		t.Fatalf("first post-Reset call adjusted target unexpectedly: %d", got)
	}
}

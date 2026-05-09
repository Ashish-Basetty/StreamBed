package bandwidth

import (
	"context"
	"sync/atomic"
	"testing"
	"time"
)

func TestComposite_PicksMin(t *testing.T) {
	a := NewRemote(1_000_000)
	b := NewRemote(500_000)
	c := NewRemote(2_000_000)
	composite := NewComposite(a, b, c)
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

func TestRemote_DefaultThenUpdate(t *testing.T) {
	r := NewRemote(123_456)
	if got := r.TargetBps(); got != 123_456 {
		t.Fatalf("default = %d, want 123_456", got)
	}
	r.Update(789_000)
	if got := r.TargetBps(); got != 789_000 {
		t.Fatalf("after Update = %d, want 789_000", got)
	}
	// Update(0) restores default to avoid wedging the estimator at zero.
	r.Update(0)
	if got := r.TargetBps(); got != 123_456 {
		t.Fatalf("Update(0) = %d, want default 123_456", got)
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

package bandwidth

import (
	"context"
	"sync/atomic"
	"time"
)

// SamplingBackend computes a smoothed rate by polling a monotonic byte counter
// at a fixed interval. EWMA-smoothed, clamped to [min, max], with a safety
// factor that leaves headroom below the observed peak.
//
// Mirrors the Python SentRateBackend's behaviour. The byte source is supplied
// as a closure so the same backend works against either an atomic.Uint64 in
// metrics or any other counter you point at it.
type SamplingBackend struct {
	source       func() uint64        // monotonic byte counter
	interval     time.Duration        // sample period
	alpha        float64              // EWMA factor on the new sample (0..1)
	safetyFactor float64              // multiplier on the observed bps (0..1]
	min, max     uint64               // clamp bounds on the smoothed result
	smoothed     atomic.Uint64        // current TargetBps()
}

// SamplingConfig captures the knobs. Zero values fall back to sensible
// defaults so most call sites can pass {} and move on.
type SamplingConfig struct {
	Interval     time.Duration
	Alpha        float64
	SafetyFactor float64
	Min          uint64
	Max          uint64
	InitialBps   uint64
}

func NewSampling(source func() uint64, cfg SamplingConfig) *SamplingBackend {
	if cfg.Interval == 0 {
		cfg.Interval = 2 * time.Second
	}
	if cfg.Alpha == 0 {
		cfg.Alpha = 0.2
	}
	if cfg.SafetyFactor == 0 {
		cfg.SafetyFactor = 0.9
	}
	if cfg.Min == 0 {
		cfg.Min = 10_000
	}
	if cfg.Max == 0 {
		cfg.Max = 50_000_000
	}
	if cfg.InitialBps == 0 {
		cfg.InitialBps = cfg.Min
	}
	b := &SamplingBackend{
		source:       source,
		interval:     cfg.Interval,
		alpha:        cfg.Alpha,
		safetyFactor: cfg.SafetyFactor,
		min:          cfg.Min,
		max:          cfg.Max,
	}
	b.smoothed.Store(cfg.InitialBps)
	return b
}

func (b *SamplingBackend) TargetBps() uint64 { return b.smoothed.Load() }

// Run samples until ctx is cancelled. Caller spawns it in a goroutine.
func (b *SamplingBackend) Run(ctx context.Context) error {
	ticker := time.NewTicker(b.interval)
	defer ticker.Stop()

	prev := b.source()
	prevTime := time.Now()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case now := <-ticker.C:
			cur := b.source()
			elapsed := now.Sub(prevTime).Seconds()
			delta := cur - prev
			prev = cur
			prevTime = now

			if elapsed <= 0 {
				continue
			}
			observed := uint64((float64(delta*8) / elapsed) * b.safetyFactor)
			b.update(observed)
		}
	}
}

func (b *SamplingBackend) update(observed uint64) {
	cur := b.smoothed.Load()
	target := uint64(b.alpha*float64(observed) + (1-b.alpha)*float64(cur))
	if target < b.min {
		target = b.min
	}
	if target > b.max {
		target = b.max
	}
	b.smoothed.Store(target)
}

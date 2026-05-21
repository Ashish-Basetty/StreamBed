package bandwidth

import (
	"sync"
	"sync/atomic"
	"time"
)

// AIMDBackend implements an additive-increase / multiplicative-decrease send-
// rate controller. TargetBps() rises by a fixed step each feedback cycle when
// the link looks healthy, and falls multiplicatively when FBCK reports the peer
// received noticeably less than we sent in the same window.
//
// Replaces the older RemoteBackend, which trusted FBCK as an absolute capacity
// estimate and could only ratchet down: the edge-side throttle also throttled
// the FBCK signal itself, so the controller locked into the post-incident
// floor. AIMD breaks that lock-in by probing upward independently of FBCK
// arithmetic; FBCK is only the brake.
type AIMDBackend struct {
	sentBytes     func() uint64
	now           func() time.Time
	target        atomic.Uint64
	delta         uint64
	beta          float64
	ratioThresh   float64
	idleSentFloor uint64
	min, max      uint64
	initialBps    uint64

	mu        sync.Mutex
	prevBytes uint64
	prevTime  time.Time
	primed    bool
}

// AIMDConfig knobs are zero-default-friendly: leave a field at its zero value
// to take the default. See NewAIMD for the defaults applied.
type AIMDConfig struct {
	Delta         uint64  // additive increase per healthy cycle (bps); default 1 Mbps
	Beta          float64 // multiplicative decrease factor (0..1]; default 0.7
	RatioThresh   float64 // received/sent below this triggers backoff; default 0.85
	IdleSentFloor uint64  // sent_bps below this is treated as idle, not lossy; default 50 kbps
	Min, Max      uint64  // clamp bounds on target; defaults 10 kbps / 50 Mbps
	InitialBps    uint64  // starting target and post-Reset target; default 20 Mbps
	Now           func() time.Time
}

func NewAIMD(sentBytes func() uint64, cfg AIMDConfig) *AIMDBackend {
	if cfg.Delta == 0 {
		cfg.Delta = 1_000_000
	}
	if cfg.Beta == 0 {
		cfg.Beta = 0.7
	}
	if cfg.RatioThresh == 0 {
		cfg.RatioThresh = 0.85
	}
	if cfg.IdleSentFloor == 0 {
		cfg.IdleSentFloor = 50_000
	}
	if cfg.Min == 0 {
		cfg.Min = 10_000
	}
	if cfg.Max == 0 {
		cfg.Max = 50_000_000
	}
	if cfg.InitialBps == 0 {
		cfg.InitialBps = 20_000_000
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	b := &AIMDBackend{
		sentBytes:     sentBytes,
		now:           cfg.Now,
		delta:         cfg.Delta,
		beta:          cfg.Beta,
		ratioThresh:   cfg.RatioThresh,
		idleSentFloor: cfg.IdleSentFloor,
		min:           cfg.Min,
		max:           cfg.Max,
		initialBps:    cfg.InitialBps,
	}
	b.target.Store(cfg.InitialBps)
	return b
}

func (b *AIMDBackend) TargetBps() uint64 { return b.target.Load() }

// Reset returns the backend to its initial state: target is restored to
// InitialBps and the sent-bytes window is re-primed on the next OnFeedback.
// Called from edge.Run on every successful (re)connect so a new peer doesn't
// inherit a stale target from the previous link.
func (b *AIMDBackend) Reset() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.target.Store(b.initialBps)
	b.primed = false
}

// OnFeedback applies one AIMD step from a peer-reported received_bps (the bps
// value parsed out of an FBCK frame). Computes sent_bps over the window since
// the previous call by diffing the sent-bytes counter, then decides:
//   - sent_bps below the idle floor → additive increase (we're not the
//     bottleneck; let the bucket grow so the app can use the link).
//   - received_bps < ratioThresh × sent_bps → multiplicative decrease (real
//     loss in the window).
//   - otherwise → additive increase.
//
// The first call after construction or Reset only primes the window; no
// adjustment is made until a second sample arrives.
func (b *AIMDBackend) OnFeedback(receivedBps uint64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.now()
	cur := b.sentBytes()
	if !b.primed {
		b.prevBytes = cur
		b.prevTime = now
		b.primed = true
		return
	}
	elapsed := now.Sub(b.prevTime).Seconds()
	dBytes := cur - b.prevBytes
	b.prevBytes = cur
	b.prevTime = now
	if elapsed <= 0 {
		return
	}
	sentBps := uint64(float64(dBytes*8) / elapsed)

	t := b.target.Load()
	switch {
	case sentBps < b.idleSentFloor:
		t = b.additive(t)
	case float64(receivedBps) < b.ratioThresh*float64(sentBps):
		t = b.multiplicative(t)
	default:
		t = b.additive(t)
	}
	b.target.Store(t)
}

func (b *AIMDBackend) additive(t uint64) uint64 {
	next := t + b.delta
	if next < b.min {
		next = b.min
	}
	if next > b.max {
		next = b.max
	}
	return next
}

func (b *AIMDBackend) multiplicative(t uint64) uint64 {
	next := uint64(float64(t) * b.beta)
	if next < b.min {
		next = b.min
	}
	if next > b.max {
		next = b.max
	}
	return next
}

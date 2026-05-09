// Package policy decides what happens to outbound payloads. The default
// `Passthrough` is a no-op; `RateLimit` enforces a target bps from an
// Estimator via a token-bucket, dropping only payloads that are safe to lose
// (video chunks). Embeddings and control messages always go through.
package policy

import (
	"sync"
	"time"

	"github.com/streambed/sidecar/internal/bandwidth"
	"github.com/streambed/sidecar/internal/common"
)

// Policy decides what to do with each outbound payload.
//
// Returning the payload unchanged means "send as-is".
// Returning nil means "drop this payload".
type Policy interface {
	OnEgress(payload []byte) []byte
}

type passthrough struct{}

func (passthrough) OnEgress(p []byte) []byte { return p }

// Passthrough is the no-op policy. Used as a default when no rate limit is
// wired and in tests that don't care about budget enforcement.
func Passthrough() Policy { return passthrough{} }

// RateLimit enforces a target bps from an Estimator using a token-bucket.
// The bucket refills at TargetBps()/8 bytes per second and is capped at
// `burst`.
//
// Droppability is tag-aware. Only CHNK (KindLossyData) is eligible to be
// dropped. EMBD (embeddings), RATE/ACTN/FBCK (control), and unknown tags
// always pass through. They **still drain the bucket** for their size so
// the cap accounts for the real bytes on the wire — if they push the bucket
// negative, subsequent CHNKs simply have less budget to compete for.
type RateLimit struct {
	estimator bandwidth.Estimator
	burst     uint64

	mu          sync.Mutex
	tokens      float64
	lastFill    time.Time
	initialized bool
}

func NewRateLimit(estimator bandwidth.Estimator, burstBytes uint64) *RateLimit {
	return &RateLimit{
		estimator: estimator,
		burst:     burstBytes,
	}
}

func (r *RateLimit) OnEgress(p []byte) []byte {
	r.mu.Lock()
	defer r.mu.Unlock()

	target := r.estimator.TargetBps()
	if target == 0 {
		// No estimate yet — pass everything. Resync the clock so when an
		// estimate arrives we don't credit the bucket for elapsed-while-idle.
		r.initialized = false
		return p
	}
	bytesPerSec := float64(target) / 8.0
	burst := r.burst
	if burst == 0 {
		burst = uint64(bytesPerSec) // ~1s of capacity at current rate
	}

	now := time.Now()
	if !r.initialized {
		r.tokens = float64(burst) // start full so brand-new conns aren't throttled
		r.lastFill = now
		r.initialized = true
	} else {
		elapsed := now.Sub(r.lastFill).Seconds()
		r.lastFill = now
		r.tokens += elapsed * bytesPerSec
		if r.tokens > float64(burst) {
			r.tokens = float64(burst)
		}
	}

	cost := float64(len(p))
	kind := common.ClassifyPrefix(p)

	if kind == common.KindLossyData {
		// CHNK only: drop if the bucket can't pay for it.
		if r.tokens < cost {
			return nil
		}
		r.tokens -= cost
		return p
	}

	// Embeddings, control, unknown: always pass. Still drain the bucket so the
	// rate cap accounts for them. Bucket can go negative; next refill catches
	// up. CHNKs that arrive while we're underwater will be dropped, which is
	// the desired backpressure.
	r.tokens -= cost
	return p
}

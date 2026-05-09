package policy

import (
	"testing"

	"github.com/streambed/sidecar/internal/common"
)

type fixedEstimator uint64

func (f fixedEstimator) TargetBps() uint64 { return uint64(f) }

func chnk(payloadSize int) []byte {
	out := make([]byte, payloadSize)
	copy(out[:4], common.TagCHNK[:])
	return out
}

func control(tag [4]byte, payloadSize int) []byte {
	out := make([]byte, payloadSize)
	copy(out[:4], tag[:])
	return out
}

func TestPassthrough_Identity(t *testing.T) {
	p := Passthrough()
	in := []byte("hello")
	got := p.OnEgress(in)
	if string(got) != "hello" {
		t.Fatalf("Passthrough mangled payload: %q", got)
	}
}

func TestRateLimit_AllowsCHNKUnderBudget(t *testing.T) {
	// 8000 bps = 1000 bytes/sec. Burst = 1000 bytes.
	p := NewRateLimit(fixedEstimator(8_000), 0)
	if got := p.OnEgress(chnk(100)); got == nil {
		t.Fatal("100-byte CHNK under burst was dropped")
	}
}

func TestRateLimit_DropsCHNKOverBudget(t *testing.T) {
	// 80 bps = 10 bytes/sec. Burst defaults to 10 bytes.
	p := NewRateLimit(fixedEstimator(80), 0)
	// Burst-sized CHNK fits.
	if got := p.OnEgress(chnk(10)); got == nil {
		t.Fatal("burst-sized CHNK should fit")
	}
	// Next CHNK exceeds remaining bucket and should drop.
	if got := p.OnEgress(chnk(100)); got != nil {
		t.Fatal("oversized CHNK was not dropped")
	}
}

func TestRateLimit_NeverDropsControl(t *testing.T) {
	// Tiny budget — 80 bps. CHNK would normally be dropped at this size.
	p := NewRateLimit(fixedEstimator(80), 0)
	// Drain the bucket with a CHNK first.
	_ = p.OnEgress(chnk(10))

	// All control kinds must pass even when the bucket is empty.
	for _, tag := range [][4]byte{common.TagRATE, common.TagACTN, common.TagFBCK} {
		if got := p.OnEgress(control(tag, 1_000)); got == nil {
			t.Fatalf("control payload %s was dropped — control must never drop", string(tag[:]))
		}
	}
}

func TestRateLimit_NeverDropsEmbeddings(t *testing.T) {
	// Same tiny budget. EMBD must pass even when the bucket is exhausted —
	// embeddings are inference output we can't recreate.
	p := NewRateLimit(fixedEstimator(80), 0)
	_ = p.OnEgress(chnk(10)) // drain
	if got := p.OnEgress(control(common.TagEMBD, 5_000)); got == nil {
		t.Fatal("EMBD payload was dropped — embeddings must never drop")
	}
}

func TestRateLimit_ControlDrainsBucket(t *testing.T) {
	// 80 bps, burst 10 bytes. Sending a 100-byte control payload should
	// drive the bucket negative, which means the next CHNK gets dropped
	// even though it'd otherwise fit.
	p := NewRateLimit(fixedEstimator(80), 0)
	got := p.OnEgress(control(common.TagRATE, 100))
	if got == nil {
		t.Fatal("control was unexpectedly dropped")
	}
	if got := p.OnEgress(chnk(5)); got != nil {
		t.Fatal("CHNK should be dropped after a heavy control payload depleted the bucket")
	}
}

func TestRateLimit_NoEstimateMeansAllow(t *testing.T) {
	p := NewRateLimit(fixedEstimator(0), 0)
	if got := p.OnEgress(chnk(10_000)); got == nil {
		t.Fatal("zero-target estimator should not drop")
	}
}

func TestRateLimit_CSTL_DroppableLikeCHNK(t *testing.T) {
	// CSTL is the user-facing lossy tag. Policy must treat it identically
	// to CHNK — eligible for drop when the bucket is empty.
	p := NewRateLimit(fixedEstimator(80), 0)
	cstl := func(n int) []byte {
		out := make([]byte, n)
		copy(out[:4], common.TagCSTL[:])
		return out
	}
	if got := p.OnEgress(cstl(10)); got == nil {
		t.Fatal("burst-sized CSTL should fit")
	}
	if got := p.OnEgress(cstl(100)); got != nil {
		t.Fatal("oversized CSTL was not dropped")
	}
}

func TestRateLimit_CSTR_NeverDropped(t *testing.T) {
	// CSTR is the user-facing reliable tag. Policy must never drop it,
	// matching EMBD.
	p := NewRateLimit(fixedEstimator(80), 0)
	_ = p.OnEgress(chnk(10)) // drain
	if got := p.OnEgress(control(common.TagCSTR, 5_000)); got == nil {
		t.Fatal("CSTR payload was dropped — reliable user data must never drop")
	}
}

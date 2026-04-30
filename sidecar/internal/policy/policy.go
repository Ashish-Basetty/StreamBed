// Package policy is the future home for sidecar-native rate adaptation. The
// PR that introduces this package keeps it as a no-op pass-through; a follow-up
// can land Go-side `should_drop_video_frame` semantics behind this same
// interface without touching transport code.
package policy

// Policy decides what to do with each outbound payload.
//
// Returning the payload unchanged means "send as-is".
// Returning nil means "drop this payload" (used for rate limiting).
// The returned slice may alias or replace the input.
type Policy interface {
	OnEgress(payload []byte) []byte
}

type passthrough struct{}

func (passthrough) OnEgress(p []byte) []byte { return p }

// Passthrough is the default policy until the semantic-sidecar PR lands.
func Passthrough() Policy { return passthrough{} }

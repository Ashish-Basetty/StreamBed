// Package common holds wire constants shared between roles.
package common

// Tag prefixes on the StreamBed datagram protocol. The sidecar dispatches on
// these to choose between the unreliable datagram channel and the reliable
// control stream, and the egress policy uses them to decide droppability.
var (
	// TagCHNK marks raw video / lossy bulk data. Rides QUIC datagrams. The
	// policy is allowed to drop these under bandwidth pressure.
	TagCHNK = [4]byte{'C', 'H', 'N', 'K'}
	// TagEMBD marks embedding / lossless bulk data. Also rides QUIC datagrams,
	// but the policy must NOT drop these — embeddings are inference output the
	// system can't recreate.
	TagEMBD = [4]byte{'E', 'M', 'B', 'D'}
	TagRATE = [4]byte{'R', 'A', 'T', 'E'}
	TagACTN = [4]byte{'A', 'C', 'T', 'N'}
	// TagFBCK marks server-originated rate observations. Payload is an 8-byte
	// big-endian uint64 of the smoothed received_bps. Travels on the control
	// stream and never escapes the sidecar — it feeds the edge's bandwidth
	// composite estimator directly.
	TagFBCK = [4]byte{'F', 'B', 'C', 'K'}
	// TagCSTL ("CSTM lossy") marks application-defined payloads that the
	// sidecar policy MAY drop under bandwidth pressure. Used by StreamBed
	// users (e.g. RL agents, telemetry) for bulk best-effort data alongside
	// CHNK. Treated identically to CHNK by the policy.
	TagCSTL = [4]byte{'C', 'S', 'T', 'L'}
	// TagCSTR ("CSTM reliable") marks application-defined payloads that
	// MUST arrive. Used for small, high-priority app messages (e.g. advisor
	// advice, control commands) alongside EMBD. Treated identically to EMBD
	// by the policy.
	TagCSTR = [4]byte{'C', 'S', 'T', 'R'}
)

// MaxDatagramPayload is the largest payload we will hand to QUIC as a datagram.
// Path MTU minus IP/UDP/QUIC framing. Mirrors shared/stream_chunks.py CHUNK_SIZE.
const MaxDatagramPayload = 1300

// ControlStreamLabel is the application label for the bidirectional reliable
// stream that carries RATE / ACTN messages. Length-prefixed framing on top.
const ControlStreamLabel = "streambed.control.v1"

// PacketKind splits two orthogonal concerns at once: which QUIC primitive
// carries the payload (datagram vs control stream), and whether the policy
// is allowed to drop it under pressure.
//
//   KindLossyData    = CHNK / CSTL = datagram, droppable
//   KindLosslessData = EMBD / CSTR = datagram, NEVER droppable
//   KindControl      = RATE/ACTN/FBCK = control stream, NEVER droppable
//   KindUnknown      = unrecognized 4-byte tag
//
// Channel routing in pumpUDPToQUIC groups Lossy + Lossless onto datagrams and
// Control onto the stream. The policy checks specifically for KindLossyData
// when deciding what to drop.
type PacketKind int

const (
	KindUnknown      PacketKind = iota
	KindLossyData                // CHNK
	KindLosslessData             // EMBD
	KindControl                  // RATE / ACTN / FBCK
)

// ClassifyPrefix peeks at the 4-byte tag and returns the kind.
func ClassifyPrefix(p []byte) PacketKind {
	if len(p) < 4 {
		return KindUnknown
	}
	var t [4]byte
	copy(t[:], p[:4])
	switch t {
	case TagCHNK, TagCSTL:
		return KindLossyData
	case TagEMBD, TagCSTR:
		return KindLosslessData
	case TagRATE, TagACTN, TagFBCK:
		return KindControl
	}
	return KindUnknown
}

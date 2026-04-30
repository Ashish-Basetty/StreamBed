// Package common holds wire constants shared between roles.
package common

// Magic prefixes on the StreamBed datagram protocol. The sidecar dispatches
// on these to choose between the unreliable datagram channel and the reliable
// control stream.
var (
	MagicCHNK = [4]byte{'C', 'H', 'N', 'K'}
	MagicRATE = [4]byte{'R', 'A', 'T', 'E'}
	MagicACTN = [4]byte{'A', 'C', 'T', 'N'}
)

// MaxDatagramPayload is the largest payload we will hand to QUIC as a datagram.
// Path MTU minus IP/UDP/QUIC framing. Mirrors shared/stream_chunks.py CHUNK_SIZE.
const MaxDatagramPayload = 1300

// ControlStreamLabel is the application label for the bidirectional reliable
// stream that carries RATE / ACTN messages. Length-prefixed framing on top.
const ControlStreamLabel = "streambed.control.v1"

// PacketKind is the routing decision after a 4-byte prefix peek.
type PacketKind int

const (
	KindUnknown PacketKind = iota
	KindData               // CHNK -> QUIC datagram
	KindControl            // RATE / ACTN -> reliable stream
)

func ClassifyPrefix(p []byte) PacketKind {
	if len(p) < 4 {
		return KindUnknown
	}
	var m [4]byte
	copy(m[:], p[:4])
	switch m {
	case MagicCHNK:
		return KindData
	case MagicRATE, MagicACTN:
		return KindControl
	}
	// JSON {"received_bps": ...} from server is the legacy feedback shape; route
	// it as control until the server is ported to RATE.
	if p[0] == '{' {
		return KindControl
	}
	return KindUnknown
}

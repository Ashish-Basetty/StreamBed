package bandwidth

import "sync/atomic"

// RemoteBackend stores the latest rate observation pushed from a peer over the
// control stream (e.g. server-side FBCK messages). No smoothing here — the
// producer is expected to do that close to the source where it has full
// counter history. This is just a typed atomic cell.
type RemoteBackend struct {
	defaultBps uint64
	latest     atomic.Uint64
}

func NewRemote(defaultBps uint64) *RemoteBackend {
	b := &RemoteBackend{defaultBps: defaultBps}
	b.latest.Store(defaultBps)
	return b
}

func (b *RemoteBackend) TargetBps() uint64 { return b.latest.Load() }

// Update overwrites the stored rate. Callers receive FBCK messages and parse
// the payload before invoking this.
func (b *RemoteBackend) Update(bps uint64) {
	if bps == 0 {
		bps = b.defaultBps
	}
	b.latest.Store(bps)
}

package server

import (
	"context"
	"encoding/binary"
	"log"
	"sync/atomic"
	"time"

	"github.com/streambed/sidecar/internal/common"
	"github.com/streambed/sidecar/internal/quictransport"
)

// feedbackInterval is the sample period for received_bps emission.
// Mirrors the old 2s Python loop. Short enough to be responsive, long enough
// that the EWMA gets meaningful samples on a slow link.
const feedbackInterval = 2 * time.Second

// feedbackEWMAAlpha is the smoothing factor on the new observation. Lower =
// stickier estimate (more lag, less noise).
const feedbackEWMAAlpha = 0.3

// pumpFeedback samples the per-peer received-bytes counter every
// feedbackInterval, EWMA-smooths the bps, and pushes an FBCK control message
// to the edge. Caller passes a closure that reads the relevant atomic counter
// — typically m.DatagramBytesRecv.Load — so unit tests can drive a fake.
func pumpFeedback(
	ctx context.Context,
	conn *quictransport.Conn,
	bytesRecv func() uint64,
) error {
	ticker := time.NewTicker(feedbackInterval)
	defer ticker.Stop()

	prev := bytesRecv()
	prevTime := time.Now()
	var smoothed atomic.Uint64

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case now := <-ticker.C:
			cur := bytesRecv()
			elapsed := now.Sub(prevTime).Seconds()
			delta := cur - prev
			prev = cur
			prevTime = now
			if elapsed <= 0 {
				continue
			}
			observed := uint64(float64(delta*8) / elapsed)
			cur64 := smoothed.Load()
			next := uint64(feedbackEWMAAlpha*float64(observed) + (1-feedbackEWMAAlpha)*float64(cur64))
			smoothed.Store(next)

			payload := encodeFBCK(next)
			if err := conn.SendControl(payload); err != nil {
				log.Printf("server: feedback send: %v", err)
			}
		}
	}
}

// encodeFBCK lays out an FBCK control frame: 4-byte tag + 8-byte BE uint64.
func encodeFBCK(bps uint64) []byte {
	out := make([]byte, 12)
	copy(out[:4], common.TagFBCK[:])
	binary.BigEndian.PutUint64(out[4:], bps)
	return out
}

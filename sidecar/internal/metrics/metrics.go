// Package metrics owns Prometheus-text counters and a periodic INFO log line.
// Operators tend to grep logs; the test framework scrapes /metrics. Both speak
// the same numbers.
package metrics

import (
	"fmt"
	"io"
	"log"
	"net/http"
	"sync/atomic"
	"time"
)

type Registry struct {
	DatagramsSent      atomic.Uint64
	DatagramsReceived  atomic.Uint64
	DatagramBytesSent  atomic.Uint64
	DatagramBytesRecv  atomic.Uint64
	StreamBytesSent    atomic.Uint64
	StreamBytesRecv    atomic.Uint64
	HandshakeStartedAt atomic.Int64 // unix nanos
	HandshakeDoneAt    atomic.Int64
	RTTNanos           atomic.Int64 // last reported smoothed RTT
}

func New() *Registry { return &Registry{} }

func (r *Registry) HandshakeMS() float64 {
	start := r.HandshakeStartedAt.Load()
	end := r.HandshakeDoneAt.Load()
	if start == 0 || end == 0 || end < start {
		return 0
	}
	return float64(end-start) / float64(time.Millisecond)
}

func (r *Registry) RTTMS() float64 {
	return float64(r.RTTNanos.Load()) / float64(time.Millisecond)
}

// ServeHTTP exposes a tiny Prometheus-text endpoint. Not a full registry —
// this is purposely the minimum surface to satisfy the test scraper.
func (r *Registry) ServeHTTP(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	r.write(w)
}

func (r *Registry) write(w io.Writer) {
	fmt.Fprintf(w, "streambed_sidecar_datagrams_sent %d\n", r.DatagramsSent.Load())
	fmt.Fprintf(w, "streambed_sidecar_datagrams_received %d\n", r.DatagramsReceived.Load())
	fmt.Fprintf(w, "streambed_sidecar_datagram_bytes_sent %d\n", r.DatagramBytesSent.Load())
	fmt.Fprintf(w, "streambed_sidecar_datagram_bytes_received %d\n", r.DatagramBytesRecv.Load())
	fmt.Fprintf(w, "streambed_sidecar_stream_bytes_sent %d\n", r.StreamBytesSent.Load())
	fmt.Fprintf(w, "streambed_sidecar_stream_bytes_received %d\n", r.StreamBytesRecv.Load())
	fmt.Fprintf(w, "streambed_sidecar_handshake_ms %f\n", r.HandshakeMS())
	fmt.Fprintf(w, "streambed_sidecar_rtt_ms %f\n", r.RTTMS())
}

// LogLoop emits a single INFO line every interval with the current snapshot.
func (r *Registry) LogLoop(interval time.Duration, role string) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for range t.C {
		log.Printf("metrics role=%s dg_sent=%d dg_recv=%d bytes_sent=%d bytes_recv=%d stream_sent=%d stream_recv=%d handshake_ms=%.1f rtt_ms=%.1f",
			role,
			r.DatagramsSent.Load(),
			r.DatagramsReceived.Load(),
			r.DatagramBytesSent.Load(),
			r.DatagramBytesRecv.Load(),
			r.StreamBytesSent.Load(),
			r.StreamBytesRecv.Load(),
			r.HandshakeMS(),
			r.RTTMS(),
		)
	}
}

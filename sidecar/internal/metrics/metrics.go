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

	BytesSentByTag map[string]*atomic.Uint64
	BytesRecvByTag map[string]*atomic.Uint64
}

// knownTags is the closed set of 4-byte payload tags the sidecar recognizes
// (mirrors sidecar/internal/common/protocol.go). UNKN is the bucket for any
// payload whose prefix doesn't match a known tag.
var knownTags = []string{"CHNK", "EMBD", "CSTL", "CSTR", "RATE", "ACTN", "FBCK", "UNKN"}

func New() *Registry {
	r := &Registry{
		BytesSentByTag: make(map[string]*atomic.Uint64, len(knownTags)),
		BytesRecvByTag: make(map[string]*atomic.Uint64, len(knownTags)),
	}
	for _, t := range knownTags {
		r.BytesSentByTag[t] = &atomic.Uint64{}
		r.BytesRecvByTag[t] = &atomic.Uint64{}
	}
	return r
}

func tagOfPrefix(p []byte) string {
	if len(p) < 4 {
		return "UNKN"
	}
	switch string(p[:4]) {
	case "CHNK", "EMBD", "CSTL", "CSTR", "RATE", "ACTN", "FBCK":
		return string(p[:4])
	}
	return "UNKN"
}

// IncBytesSentByTag adds n to the per-tag sent counter inferred from the
// payload's 4-byte prefix. Safe to call from any goroutine.
func (r *Registry) IncBytesSentByTag(p []byte, n int) {
	if c, ok := r.BytesSentByTag[tagOfPrefix(p)]; ok {
		c.Add(uint64(n))
	}
}

// IncBytesRecvByTag adds n to the per-tag received counter inferred from the
// payload's 4-byte prefix.
func (r *Registry) IncBytesRecvByTag(p []byte, n int) {
	if c, ok := r.BytesRecvByTag[tagOfPrefix(p)]; ok {
		c.Add(uint64(n))
	}
}

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
	for _, t := range knownTags {
		fmt.Fprintf(w, "streambed_sidecar_bytes_sent{tag=%q} %d\n", t, r.BytesSentByTag[t].Load())
		fmt.Fprintf(w, "streambed_sidecar_bytes_received{tag=%q} %d\n", t, r.BytesRecvByTag[t].Load())
	}
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

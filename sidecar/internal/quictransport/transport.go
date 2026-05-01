// Package quictransport wraps quic-go for the sidecar. It owns connection
// lifecycle, datagram I/O, and the single bidirectional control stream.
//
// The transport is role-agnostic: edge and server both use it.
package quictransport

import (
	"context"
	"crypto/tls"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/quic-go/quic-go"

	"github.com/streambed/sidecar/internal/metrics"
)

// ALPN identifies the protocol both ends agree on. Mismatch -> handshake fails.
const ALPN = "streambed-quic-v1"

// Conn is the duplex view both sides use after handshake.
type Conn struct {
	q    quic.Connection
	ctrl quic.Stream
	m    *metrics.Registry
}

// SendDatagram pushes an unreliable, unordered payload. Returns ErrTooLarge if
// payload exceeds the negotiated datagram MTU (caller should fragment first;
// the StreamBed CHNK framing already does this).
func (c *Conn) SendDatagram(p []byte) error {
	if c.q == nil {
		return errors.New("transport: not connected")
	}
	err := c.q.SendDatagram(p)
	if err == nil {
		c.m.DatagramsSent.Add(1)
		c.m.DatagramBytesSent.Add(uint64(len(p)))
	}
	return err
}

// RecvDatagram blocks for the next datagram. Returns io.EOF when the
// connection closes cleanly.
func (c *Conn) RecvDatagram(ctx context.Context) ([]byte, error) {
	p, err := c.q.ReceiveDatagram(ctx)
	if err != nil {
		return nil, err
	}
	c.m.DatagramsReceived.Add(1)
	c.m.DatagramBytesRecv.Add(uint64(len(p)))
	return p, nil
}

// SendControl writes a length-prefixed frame on the reliable control stream.
// Each call is one logical message; the framing keeps the receiver's parsing
// simple and avoids needing a delimiter byte in JSON payloads.
func (c *Conn) SendControl(p []byte) error {
	if c.ctrl == nil {
		return errors.New("transport: control stream not open")
	}
	var hdr [4]byte
	binary.BigEndian.PutUint32(hdr[:], uint32(len(p)))
	if _, err := c.ctrl.Write(hdr[:]); err != nil {
		return err
	}
	if _, err := c.ctrl.Write(p); err != nil {
		return err
	}
	c.m.StreamBytesSent.Add(uint64(4 + len(p)))
	return nil
}

// RecvControl reads exactly one length-prefixed frame from the control stream.
func (c *Conn) RecvControl() ([]byte, error) {
	var hdr [4]byte
	if _, err := io.ReadFull(c.ctrl, hdr[:]); err != nil {
		return nil, err
	}
	n := binary.BigEndian.Uint32(hdr[:])
	if n > 1<<20 {
		return nil, fmt.Errorf("control frame too large: %d", n)
	}
	buf := make([]byte, n)
	if _, err := io.ReadFull(c.ctrl, buf); err != nil {
		return nil, err
	}
	c.m.StreamBytesRecv.Add(uint64(4 + n))
	return buf, nil
}

func (c *Conn) Close() error {
	if c.q == nil {
		return nil
	}
	return c.q.CloseWithError(0, "shutdown")
}

func (c *Conn) PollRTT() {
	stats := c.q.Context() // placeholder hook; quic-go exposes stats via internal types
	_ = stats
}

// Dial opens a QUIC connection (edge role). It also opens the single
// bidirectional control stream so both sides agree on its identity.
func Dial(ctx context.Context, peerAddr string, tlsCfg *tls.Config, m *metrics.Registry) (*Conn, error) {
	tlsCfg = withALPN(tlsCfg)
	cfg := &quic.Config{
		EnableDatagrams:    true,
		MaxIdleTimeout:     60 * time.Second,
		KeepAlivePeriod:    10 * time.Second,
		HandshakeIdleTimeout: 10 * time.Second,
	}
	m.HandshakeStartedAt.Store(time.Now().UnixNano())
	q, err := quic.DialAddr(ctx, peerAddr, tlsCfg, cfg)
	if err != nil {
		return nil, fmt.Errorf("quic dial %s: %w", peerAddr, err)
	}
	m.HandshakeDoneAt.Store(time.Now().UnixNano())
	stream, err := q.OpenStreamSync(ctx)
	if err != nil {
		_ = q.CloseWithError(1, "open control stream")
		return nil, fmt.Errorf("open control stream: %w", err)
	}
	// Materialize the stream on the wire by writing an empty length-prefixed
	// frame. quic-go's AcceptStream on the peer only fires once a STREAM frame
	// arrives, and OpenStreamSync alone does not flush until data is written.
	var initHdr [4]byte
	if _, err := stream.Write(initHdr[:]); err != nil {
		_ = q.CloseWithError(1, "init control stream")
		return nil, fmt.Errorf("init control stream: %w", err)
	}
	return &Conn{q: q, ctrl: stream, m: m}, nil
}

// Listen accepts a single QUIC connection (server role) and waits for the peer
// to open the control stream. Kept for callers that only want one peer; for
// multi-peer use ListenAll.
func Listen(ctx context.Context, bindAddr string, tlsCfg *tls.Config, m *metrics.Registry) (*Conn, error) {
	ln, err := openListener(bindAddr, tlsCfg)
	if err != nil {
		return nil, err
	}
	return acceptOne(ctx, ln, m)
}

// Listener wraps a quic.Listener so callers can repeatedly accept connections.
type Listener struct {
	ln *quic.Listener
}

func (l *Listener) Close() error { return l.ln.Close() }

// Accept blocks for the next peer connection and opens its control stream.
// Each accepted Conn carries a fresh metrics.Registry slot if the caller passes
// one; otherwise pass nil and the connection metrics are dropped.
func (l *Listener) Accept(ctx context.Context, m *metrics.Registry) (*Conn, error) {
	if m == nil {
		m = metrics.New()
	}
	return acceptOne(ctx, l.ln, m)
}

// ListenAll opens a QUIC listener that can repeatedly accept peers. Caller is
// responsible for spawning a goroutine per accepted Conn.
func ListenAll(bindAddr string, tlsCfg *tls.Config) (*Listener, error) {
	ln, err := openListener(bindAddr, tlsCfg)
	if err != nil {
		return nil, err
	}
	return &Listener{ln: ln}, nil
}

func openListener(bindAddr string, tlsCfg *tls.Config) (*quic.Listener, error) {
	tlsCfg = withALPN(tlsCfg)
	cfg := &quic.Config{
		EnableDatagrams:      true,
		MaxIdleTimeout:       60 * time.Second,
		KeepAlivePeriod:      10 * time.Second,
		HandshakeIdleTimeout: 10 * time.Second,
	}
	ln, err := quic.ListenAddr(bindAddr, tlsCfg, cfg)
	if err != nil {
		return nil, fmt.Errorf("quic listen %s: %w", bindAddr, err)
	}
	return ln, nil
}

func acceptOne(ctx context.Context, ln *quic.Listener, m *metrics.Registry) (*Conn, error) {
	m.HandshakeStartedAt.Store(time.Now().UnixNano())
	q, err := ln.Accept(ctx)
	if err != nil {
		return nil, fmt.Errorf("quic accept: %w", err)
	}
	m.HandshakeDoneAt.Store(time.Now().UnixNano())
	stream, err := q.AcceptStream(ctx)
	if err != nil {
		_ = q.CloseWithError(1, "accept control stream")
		return nil, fmt.Errorf("accept control stream: %w", err)
	}
	// Consume the 4-byte init frame the edge writes on Dial. See Dial for why.
	var initHdr [4]byte
	if _, err := io.ReadFull(stream, initHdr[:]); err != nil {
		_ = q.CloseWithError(1, "read init control frame")
		return nil, fmt.Errorf("read init control frame: %w", err)
	}
	return &Conn{q: q, ctrl: stream, m: m}, nil
}

func withALPN(t *tls.Config) *tls.Config {
	out := t.Clone()
	out.NextProtos = []string{ALPN}
	return out
}

// Package server implements the server-side sidecar role:
//
//	peer sidecar -> QUIC -> [server sidecar] -> UDP localhost -> server container
//
// Receives QUIC datagrams + control-stream messages and re-emits them as UDP
// to the local server inference container, which is unmodified.
//
// Optional reverse path: when LocalUDPBindAddr is set, the server sidecar
// listens on that UDP port and pumps any received packets back over the QUIC
// control stream. This is the same channel pumpFeedback uses for FBCK, so
// edge-side dispatch stays a single switch on the 4-byte tag — small CSTR
// payloads (advisor advice) ride alongside the existing feedback flow.
package server

import (
	"context"
	"log"
	"net"
	"time"

	"github.com/streambed/sidecar/internal/metrics"
	"github.com/streambed/sidecar/internal/quictransport"
)

type Config struct {
	BindAddr           string // "0.0.0.0:4433" — public QUIC port
	LocalServerUDPAddr string // "127.0.0.1:9000" — server container's listen
	// LocalUDPBindAddr is the local UDP listen for reverse-path data
	// originating in the server-side app (e.g. advisor CSTR). Empty disables
	// the listener; FBCK keeps flowing either way.
	LocalUDPBindAddr string
	Metrics          *metrics.Registry
}

// Run accepts QUIC connections on BindAddr forever, spawning a per-peer pump
// for each one. Each peer is its own QUIC connection with its own congestion
// control — peer fairness is up to QUIC.
func Run(ctx context.Context, cfg Config) error {
	if cfg.Metrics == nil {
		cfg.Metrics = metrics.New()
	}
	tlsCfg, err := quictransport.DevTLSConfig(hostOf(cfg.BindAddr), true)
	if err != nil {
		return err
	}
	ln, err := quictransport.ListenAll(cfg.BindAddr, tlsCfg)
	if err != nil {
		return err
	}
	defer ln.Close()
	log.Printf("server: listening on %s; forwarding to %s", cfg.BindAddr, cfg.LocalServerUDPAddr)

	// Optional reverse-path UDP listener: shared across peers. Each datagram
	// read from here is fanned out to the most-recently-connected peer's
	// control stream. With one peer (the advisor case) this is exact; with
	// multiple peers competing for the same listener, latest-wins. Per-peer
	// listeners would need distinct ports; not worth the plumbing today.
	var revIn *net.UDPConn
	if cfg.LocalUDPBindAddr != "" {
		raddr, rerr := net.ResolveUDPAddr("udp", cfg.LocalUDPBindAddr)
		if rerr != nil {
			return rerr
		}
		revIn, rerr = net.ListenUDP("udp", raddr)
		if rerr != nil {
			return rerr
		}
		defer revIn.Close()
		log.Printf("server: reverse-path UDP listening on %s (forwarded as control frames)", revIn.LocalAddr())
	}

	for {
		conn, err := ln.Accept(ctx, cfg.Metrics)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			log.Printf("server: accept failed: %v", err)
			return err
		}
		log.Printf("server: peer connected")
		// NOTE: cfg.Metrics is currently shared across peers (set in main.go),
		// so DatagramBytesRecv is the cluster-wide counter. With one peer this
		// is fine; for true per-peer feedback we'd need ln.Accept(ctx, nil) and
		// hold the new registry here. Left as a follow-up.
		go handlePeer(ctx, conn, cfg.LocalServerUDPAddr, cfg.Metrics.DatagramBytesRecv.Load, revIn)
	}
}

func handlePeer(
	ctx context.Context,
	conn *quictransport.Conn,
	localServerUDPAddr string,
	bytesRecv func() uint64,
	revIn *net.UDPConn,
) {
	defer conn.Close()

	// Retry DNS resolution until the inference container registers its alias.
	var localAddr *net.UDPAddr
	for {
		var err error
		localAddr, err = net.ResolveUDPAddr("udp", localServerUDPAddr)
		if err == nil {
			break
		}
		log.Printf("server: waiting for %s (%v)", localServerUDPAddr, err)
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Second):
		}
	}
	out, err := net.DialUDP("udp", nil, localAddr)
	if err != nil {
		log.Printf("server: dial local udp: %v", err)
		return
	}
	defer out.Close()

	errc := make(chan error, 4)
	go func() { errc <- pumpDatagramsToUDP(ctx, conn, out) }()
	go func() { errc <- pumpControlToUDP(ctx, conn, out) }()
	go func() { errc <- pumpFeedback(ctx, conn, bytesRecv) }()
	if revIn != nil {
		go func() { errc <- pumpUDPToControl(ctx, revIn, conn) }()
	}

	select {
	case <-ctx.Done():
		return
	case e := <-errc:
		log.Printf("server: peer pump exited: %v", e)
	}
}

// pumpUDPToControl reads packets from the reverse-path UDP listener and
// forwards each one verbatim onto the QUIC control stream. The edge sidecar's
// control receive loop dispatches by 4-byte tag — FBCK feeds bandwidth, the
// rest gets forwarded out to the local app's UDP listener (when configured).
func pumpUDPToControl(ctx context.Context, in *net.UDPConn, conn *quictransport.Conn) error {
	buf := make([]byte, 65535)
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		n, _, err := in.ReadFromUDP(buf)
		if err != nil {
			return err
		}
		if err := conn.SendControl(buf[:n]); err != nil {
			log.Printf("server: reverse-path control send: %v", err)
		}
	}
}

func pumpDatagramsToUDP(ctx context.Context, conn *quictransport.Conn, out *net.UDPConn) error {
	for {
		p, err := conn.RecvDatagram(ctx)
		if err != nil {
			return err
		}
		if _, err := out.Write(p); err != nil {
			log.Printf("server: udp write: %v", err)
		}
	}
}

func pumpControlToUDP(ctx context.Context, conn *quictransport.Conn, out *net.UDPConn) error {
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		msg, err := conn.RecvControl()
		if err != nil {
			return err
		}
		if _, err := out.Write(msg); err != nil {
			log.Printf("server: control->udp write: %v", err)
		}
	}
}

func hostOf(addrPort string) string {
	host, _, err := net.SplitHostPort(addrPort)
	if err != nil {
		return addrPort
	}
	return host
}

// Package server implements the server-side sidecar role:
//
//   peer sidecar -> QUIC -> [server sidecar] -> UDP localhost -> server container
//
// Receives QUIC datagrams + control-stream messages and re-emits them as UDP
// to the local server inference container, which is unmodified.
//
// There is no reverse path today: the server emits no feedback. If RATE/ACTN
// or future server->edge messages get a producer, add a pump here that reads
// from a server-local UDP listener and calls conn.SendControl().
package server

import (
	"context"
	"log"
	"net"

	"github.com/streambed/sidecar/internal/metrics"
	"github.com/streambed/sidecar/internal/quictransport"
)

type Config struct {
	BindAddr           string // "0.0.0.0:4433" — public QUIC port
	LocalServerUDPAddr string // "127.0.0.1:9000" — server container's listen
	Metrics            *metrics.Registry
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

	localAddr, err := net.ResolveUDPAddr("udp", cfg.LocalServerUDPAddr)
	if err != nil {
		return err
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
		go handlePeer(ctx, conn, localAddr, cfg.Metrics.DatagramBytesRecv.Load)
	}
}

func handlePeer(ctx context.Context, conn *quictransport.Conn, localAddr *net.UDPAddr, bytesRecv func() uint64) {
	defer conn.Close()

	out, err := net.DialUDP("udp", nil, localAddr)
	if err != nil {
		log.Printf("server: dial local udp: %v", err)
		return
	}
	defer out.Close()

	errc := make(chan error, 3)
	go func() { errc <- pumpDatagramsToUDP(ctx, conn, out) }()
	go func() { errc <- pumpControlToUDP(ctx, conn, out) }()
	go func() { errc <- pumpFeedback(ctx, conn, bytesRecv) }()

	select {
	case <-ctx.Done():
		return
	case e := <-errc:
		log.Printf("server: peer pump exited: %v", e)
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

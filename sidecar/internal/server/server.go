// Package server implements the server-side sidecar role:
//
//	peer sidecar -> QUIC -> [server sidecar] <-> UDP (single port) <-> server container
//
// Inference dials the sidecar on LocalUDPBind; the sidecar records the last
// source address and uses WriteToUDP for QUIC->app. Ingress UDP from the
// app is forwarded onto the QUIC control stream (same framing as the legacy
// reverse listener).
package server

import (
	"context"
	"log"
	"net"
	"sync"
	"sync/atomic"

	"github.com/streambed/sidecar/internal/metrics"
	"github.com/streambed/sidecar/internal/quictransport"
)

type Config struct {
	BindAddr     string // "0.0.0.0:4433" — public QUIC port
	LocalUDPBind string // "0.0.0.0:9050" — single UDP port for app <-> sidecar
	Metrics      *metrics.Registry
}

// Run accepts QUIC connections on BindAddr. A single UDP socket on LocalUDPBind
// carries both directions: the inference app is expected to originate traffic so
// the sidecar learns its address; QUIC-originated payloads are written back with
// WriteToUDP to that address.
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

	udpAddr, err := net.ResolveUDPAddr("udp", cfg.LocalUDPBind)
	if err != nil {
		return err
	}
	udpConn, err := net.ListenUDP("udp", udpAddr)
	if err != nil {
		return err
	}
	defer udpConn.Close()

	var lastInfer atomic.Pointer[net.UDPAddr]
	var mu sync.Mutex
	var activeConn *quictransport.Conn

	// App -> QUIC (single reader on the unified UDP socket).
	go func() {
		buf := make([]byte, 65535)
		for {
			select {
			case <-ctx.Done():
				return
			default:
			}
			n, addr, rerr := udpConn.ReadFromUDP(buf)
			if rerr != nil {
				if ctx.Err() != nil {
					return
				}
				log.Printf("server: unified UDP read: %v", rerr)
				continue
			}
			if addr != nil {
				a := *addr
				lastInfer.Store(&a)
			}
			mu.Lock()
			c := activeConn
			mu.Unlock()
			if c == nil {
				continue
			}
			payload := append([]byte(nil), buf[:n]...)
			if err := c.SendControl(payload); err != nil {
				log.Printf("server: app->QUIC control send: %v", err)
			}
		}
	}()

	log.Printf("server: listening QUIC %s; unified UDP %s", cfg.BindAddr, cfg.LocalUDPBind)

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
		mu.Lock()
		activeConn = conn
		mu.Unlock()
		go func(qc *quictransport.Conn) {
			defer qc.Close()
			defer func() {
				mu.Lock()
				if activeConn == qc {
					activeConn = nil
				}
				mu.Unlock()
			}()
			handlePeer(ctx, qc, udpConn, &lastInfer, cfg.Metrics.DatagramBytesRecv.Load)
		}(conn)
	}
}

func handlePeer(
	ctx context.Context,
	conn *quictransport.Conn,
	udpConn *net.UDPConn,
	lastInfer *atomic.Pointer[net.UDPAddr],
	bytesRecv func() uint64,
) {
	errc := make(chan error, 3)
	go func() { errc <- pumpDatagramsToUDP(ctx, conn, udpConn, lastInfer) }()
	go func() { errc <- pumpControlToUDP(ctx, conn, udpConn, lastInfer) }()
	go func() { errc <- pumpFeedback(ctx, conn, bytesRecv) }()

	select {
	case <-ctx.Done():
	case e := <-errc:
		log.Printf("server: peer pump exited: %v", e)
	}
}

func pumpDatagramsToUDP(ctx context.Context, conn *quictransport.Conn, udp *net.UDPConn, lastInfer *atomic.Pointer[net.UDPAddr]) error {
	for {
		p, err := conn.RecvDatagram(ctx)
		if err != nil {
			return err
		}
		dst := lastInfer.Load()
		if dst == nil {
			continue
		}
		if _, err := udp.WriteToUDP(p, dst); err != nil {
			log.Printf("server: udp write: %v", err)
		}
	}
}

func pumpControlToUDP(ctx context.Context, conn *quictransport.Conn, udp *net.UDPConn, lastInfer *atomic.Pointer[net.UDPAddr]) error {
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		msg, err := conn.RecvControl()
		if err != nil {
			return err
		}
		dst := lastInfer.Load()
		if dst == nil {
			continue
		}
		if _, err := udp.WriteToUDP(msg, dst); err != nil {
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

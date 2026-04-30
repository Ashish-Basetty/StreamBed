// Package edge implements the edge-side sidecar role:
//
//   daemon -> UDP localhost -> [edge sidecar] -> QUIC -> peer sidecar
//
// Read local UDP, classify by 4-byte magic, push CHNK as datagrams and
// RATE/ACTN/JSON-feedback over the control stream.
package edge

import (
	"context"
	"errors"
	"log"
	"net"

	"github.com/streambed/sidecar/internal/common"
	"github.com/streambed/sidecar/internal/metrics"
	"github.com/streambed/sidecar/internal/policy"
	"github.com/streambed/sidecar/internal/quictransport"
)

type Config struct {
	LocalUDPBind  string // "0.0.0.0:9050"
	PeerAddr      string // "server-sidecar:4433"
	DaemonAddr    string // host:port to forward server-originated feedback to
	TLS           any    // *tls.Config kept generic to avoid stdlib import here
	Metrics       *metrics.Registry
	Policy        policy.Policy
}

func Run(ctx context.Context, cfg Config) error {
	if cfg.Policy == nil {
		cfg.Policy = policy.Passthrough()
	}
	if cfg.Metrics == nil {
		cfg.Metrics = metrics.New()
	}

	udpAddr, err := net.ResolveUDPAddr("udp", cfg.LocalUDPBind)
	if err != nil {
		return err
	}
	udp, err := net.ListenUDP("udp", udpAddr)
	if err != nil {
		return err
	}
	defer udp.Close()
	log.Printf("edge: local UDP bound on %s, dialing peer %s", udp.LocalAddr(), cfg.PeerAddr)

	tlsCfg, err := quictransport.DevTLSConfig(hostOf(cfg.PeerAddr), false)
	if err != nil {
		return err
	}
	conn, err := quictransport.Dial(ctx, cfg.PeerAddr, tlsCfg, cfg.Metrics)
	if err != nil {
		return err
	}
	defer conn.Close()
	log.Printf("edge: QUIC handshake complete to %s", cfg.PeerAddr)

	// Daemon address is where we forward server-originated feedback to.
	daemonAddr, err := net.ResolveUDPAddr("udp", cfg.DaemonAddr)
	if err != nil {
		return err
	}

	errc := make(chan error, 2)
	go func() { errc <- pumpUDPToQUIC(ctx, udp, conn, cfg) }()
	go func() { errc <- pumpControlToUDP(ctx, conn, udp, daemonAddr, cfg.Metrics) }()

	select {
	case <-ctx.Done():
		return ctx.Err()
	case e := <-errc:
		return e
	}
}

func pumpUDPToQUIC(ctx context.Context, udp *net.UDPConn, conn *quictransport.Conn, cfg Config) error {
	buf := make([]byte, 65535)
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		n, _, err := udp.ReadFromUDP(buf)
		if err != nil {
			return err
		}
		payload := cfg.Policy.OnEgress(buf[:n])
		if payload == nil {
			continue
		}
		switch common.ClassifyPrefix(payload) {
		case common.KindData:
			if err := conn.SendDatagram(payload); err != nil {
				log.Printf("edge: send datagram: %v", err)
			}
		case common.KindControl:
			if err := conn.SendControl(payload); err != nil {
				log.Printf("edge: send control: %v", err)
			}
		default:
			// Best-effort: unclassified payloads ride the datagram channel.
			if err := conn.SendDatagram(payload); err != nil {
				log.Printf("edge: send datagram (unclassified): %v", err)
			}
		}
	}
}

func pumpControlToUDP(ctx context.Context, conn *quictransport.Conn, udp *net.UDPConn, daemon *net.UDPAddr, m *metrics.Registry) error {
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		msg, err := conn.RecvControl()
		if err != nil {
			if errors.Is(err, context.Canceled) {
				return err
			}
			return err
		}
		if _, err := udp.WriteToUDP(msg, daemon); err != nil {
			log.Printf("edge: forward feedback to daemon: %v", err)
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

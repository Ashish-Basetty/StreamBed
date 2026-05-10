// Package edge implements the edge-side sidecar role:
//
//	daemon -> UDP localhost -> [edge sidecar] -> QUIC -> peer sidecar
//
// Read local UDP, classify by 4-byte magic, push CHNK as datagrams and
// RATE/ACTN over the control stream. The reverse direction carries server
// FBCK observations into the bandwidth.RemoteBackend, plus (when
// LocalRecvUDPTarget is set) any non-FBCK control payloads — e.g. CSTR
// advice from a server-side advisor — forwarded back out to a local UDP
// destination. Rate enforcement is fully sidecar-internal.
package edge

import (
	"context"
	"encoding/binary"
	"log"
	"net"

	"github.com/streambed/sidecar/internal/bandwidth"
	"github.com/streambed/sidecar/internal/common"
	"github.com/streambed/sidecar/internal/metrics"
	"github.com/streambed/sidecar/internal/policy"
	"github.com/streambed/sidecar/internal/quictransport"
)

type Config struct {
	LocalUDPBind string // "0.0.0.0:9050"
	PeerAddr     string // "server-sidecar:4433"
	// LocalRecvUDPTarget is an optional "host:port" the edge sidecar will
	// forward non-FBCK control messages to (e.g. CSTR advice originating from
	// a server-side advisor). Empty disables the reverse-data path; FBCK keeps
	// flowing into the bandwidth estimator either way.
	LocalRecvUDPTarget string
	TLS                any // *tls.Config kept generic to avoid stdlib import here
	Metrics            *metrics.Registry
	// Policy gates outbound payloads. If nil, Run constructs a
	// RateLimit policy backed by a Composite(SentRate, RemoteRate) estimator.
	Policy policy.Policy
}

func Run(ctx context.Context, cfg Config) error {
	if cfg.Metrics == nil {
		cfg.Metrics = metrics.New()
	}

	// Local observation: rate of bytes we actually push out as QUIC datagrams.
	// Sampled from the same atomic counter the metrics endpoint exposes.
	sentRate := bandwidth.NewSampling(cfg.Metrics.DatagramBytesSent.Load, bandwidth.SamplingConfig{})

	// Remote observation: server-reported received_bps from FBCK frames.
	remoteRate := bandwidth.NewRemote(500_000)

	estimator := bandwidth.NewComposite(sentRate, remoteRate)

	if cfg.Policy == nil {
		cfg.Policy = policy.NewRateLimit(estimator, 0)
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

	errc := make(chan error, 3)
	go func() { errc <- pumpUDPToQUIC(ctx, udp, conn, cfg) }()
	go func() { errc <- sentRate.Run(ctx) }()
	go func() { errc <- pumpControlIntoBandwidth(ctx, conn, remoteRate, cfg.LocalRecvUDPTarget) }()

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
		case common.KindLossyData, common.KindLosslessData:
			// CHNK + EMBD both ride the datagram channel — bulk, low-latency,
			// best-effort. Drop policy is enforced upstream (CHNK only).
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

// pumpControlIntoBandwidth reads control frames from the peer and dispatches
// by magic. FBCK frames update the RemoteBackend. Non-FBCK frames are
// forwarded to recvTarget (resolved lazily on first use) as raw UDP — this is
// how server-side CSTR (e.g. advisor advice) reaches the local app. Lazy
// resolution avoids startup failures when the inference container's DNS alias
// isn't registered yet.
func pumpControlIntoBandwidth(
	ctx context.Context,
	conn *quictransport.Conn,
	remote *bandwidth.RemoteBackend,
	recvTarget string,
) error {
	var recvOut *net.UDPConn
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		msg, err := conn.RecvControl()
		if err != nil {
			return err
		}
		if len(msg) < 4 {
			continue
		}
		var t [4]byte
		copy(t[:], msg[:4])
		switch t {
		case common.TagFBCK:
			if len(msg) < 12 {
				log.Printf("edge: short FBCK frame (%d bytes)", len(msg))
				continue
			}
			bps := binary.BigEndian.Uint64(msg[4:12])
			remote.Update(bps)
		default:
			// CSTR / RATE / ACTN bound for the local app. Resolve and dial on
			// first use so the sidecar can start before the inference container
			// is registered in Docker DNS.
			if recvTarget == "" {
				continue
			}
			if recvOut == nil {
				raddr, rerr := net.ResolveUDPAddr("udp", recvTarget)
				if rerr != nil {
					log.Printf("edge: reverse-path resolve %s: %v (dropping)", recvTarget, rerr)
					continue
				}
				recvOut, rerr = net.DialUDP("udp", nil, raddr)
				if rerr != nil {
					log.Printf("edge: reverse-path dial %s: %v (dropping)", recvTarget, rerr)
					continue
				}
				log.Printf("edge: reverse-path UDP -> %s", raddr)
			}
			if _, werr := recvOut.Write(msg); werr != nil {
				log.Printf("edge: reverse-path UDP write: %v", werr)
			}
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

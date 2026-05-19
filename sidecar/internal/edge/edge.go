// Package edge implements the edge-side sidecar role:
//
//	daemon -> UDP localhost -> [edge sidecar] -> QUIC -> peer sidecar
//
// Read local UDP, classify by 4-byte magic, push CHNK as datagrams and
// RATE/ACTN over the control stream. The reverse direction carries server
// FBCK observations into the bandwidth.RemoteBackend, plus any non-FBCK
// control payloads — e.g. CSTR advice from a server-side advisor — forwarded
// back out on the same UDP socket to the last source address from the local
// app. Rate enforcement is fully sidecar-internal.
package edge

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/streambed/sidecar/internal/bandwidth"
	"github.com/streambed/sidecar/internal/common"
	"github.com/streambed/sidecar/internal/metrics"
	"github.com/streambed/sidecar/internal/policy"
	"github.com/streambed/sidecar/internal/quictransport"
)

type Config struct {
	LocalUDPBind string // "0.0.0.0:9050"
	// DaemonURL is the base HTTP URL of the co-located deployment daemon
	// (e.g. "http://streambed-daemon-edge1:9090"). Polled every 15 s for the
	// current peer address via GET /stream-target.
	DaemonURL    string
	PeerQUICPort int // QUIC port on the server sidecar; peer = target_ip:PeerQUICPort
	TLS          any // reserved; unused today
	Metrics      *metrics.Registry
	// Policy gates outbound payloads. If nil, Run constructs a
	// RateLimit policy backed by a Composite(SentRate, RemoteRate) estimator.
	Policy policy.Policy
}

// Run binds local UDP, waits for the daemon to publish an initial peer address,
// then streams datagrams to that peer. When the daemon target changes (detected
// by the 15 s poll) or the connection drops, the current QUIC connection is
// torn down and a new one is dialed — the container never restarts.
func Run(ctx context.Context, cfg Config) error {
	if cfg.Metrics == nil {
		cfg.Metrics = metrics.New()
	}

	// Seed SentRate's initial estimate to match RemoteRate's default. Otherwise
	// Composite's min() pins the policy to SamplingConfig.Min (10 kbps) for the
	// first ~2 s, collapsing the bucket to ~1250 bytes and dropping every CHNK
	// chunk after the first regardless of actual link capacity. EWMA pulls
	// SentRate toward the true observed rate as samples arrive.
	const initialBps = 500_000
	sentRate := bandwidth.NewSampling(cfg.Metrics.DatagramBytesSent.Load, bandwidth.SamplingConfig{
		InitialBps: initialBps,
	})
	remoteRate := bandwidth.NewRemote(initialBps)
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

	rerouteCh := make(chan string, 1)
	go pollDaemonTarget(ctx, cfg.DaemonURL, cfg.PeerQUICPort, rerouteCh)
	// sentRate samples bytes-sent for the lifetime of the sidecar, not per connection.
	go func() { _ = sentRate.Run(ctx) }()

	log.Printf("edge: local UDP bound on %s, waiting for daemon target at %s/stream-target", udp.LocalAddr(), cfg.DaemonURL)

	// Block until the poll goroutine delivers the first valid peer address.
	var peerAddr string
	select {
	case <-ctx.Done():
		return ctx.Err()
	case peerAddr = <-rerouteCh:
		log.Printf("edge: initial peer %s", peerAddr)
	}

	for {
		tlsCfg, err := quictransport.DevTLSConfig(hostOf(peerAddr), false)
		if err != nil {
			return err
		}
		conn, err := quictransport.Dial(ctx, peerAddr, tlsCfg, cfg.Metrics)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			log.Printf("edge: dial %s failed: %v; retrying in 2s", peerAddr, err)
			select {
			case <-ctx.Done():
				return ctx.Err()
			case newPeer := <-rerouteCh:
				peerAddr = newPeer
			case <-time.After(2 * time.Second):
			}
			continue
		}
		log.Printf("edge: QUIC connected to %s", peerAddr)

		connCtx, connCancel := context.WithCancel(ctx)
		var lastAppAddr atomic.Pointer[net.UDPAddr]
		errc := make(chan error, 2)
		go func() { errc <- pumpUDPToQUIC(connCtx, udp, conn, cfg, &lastAppAddr) }()
		go func() { errc <- pumpControlIntoBandwidth(connCtx, conn, remoteRate, udp, &lastAppAddr, cfg.Metrics) }()

		var newPeer string
		select {
		case <-ctx.Done():
			connCancel()
			conn.Close()
			return ctx.Err()
		case newPeer = <-rerouteCh:
			log.Printf("edge: rerouting -> %s", newPeer)
		case e := <-errc:
			if ctx.Err() != nil {
				connCancel()
				conn.Close()
				return ctx.Err()
			}
			log.Printf("edge: pump exited (%v), reconnecting to %s", e, peerAddr)
		}

		connCancel()
		conn.Close()
		// Drain the second pump before starting new goroutines to avoid two
		// concurrent readers on the same UDP socket.
		select {
		case <-errc:
		case <-time.After(2 * time.Second):
		}

		if newPeer != "" {
			peerAddr = newPeer
		}
	}
}

// pollDaemonTarget polls DaemonURL/stream-target every 15 s and sends a new
// peer "ip:port" on ch whenever the resolved peer address changes. Fires once
// immediately on startup so the first connection does not wait a full interval.
//
// The JSON response includes a "target_port" field carrying the peer sidecar's
// published host UDP port (host-port addressing under per-device networks).
// peerQUICPort is the legacy fallback used only if the daemon omits the port.
func pollDaemonTarget(ctx context.Context, daemonURL string, peerQUICPort int, ch chan<- string) {
	url := daemonURL + "/stream-target"
	client := &http.Client{Timeout: 5 * time.Second}
	var currentPeer string

	check := func() {
		ip, port := fetchTarget(ctx, url, client)
		if ip == "" {
			return
		}
		if port == 0 {
			port = peerQUICPort
		}
		peer := fmt.Sprintf("%s:%d", ip, port)
		if peer == currentPeer {
			return
		}
		currentPeer = peer
		select {
		case ch <- peer:
		default: // previous reroute still pending; will be picked up next loop
		}
	}

	check()
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			check()
		}
	}
}

func fetchTarget(ctx context.Context, url string, client *http.Client) (ip string, quicPort int) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return "", 0
	}
	resp, err := client.Do(req)
	if err != nil {
		log.Printf("edge: poll daemon %s: %v", url, err)
		return "", 0
	}
	defer resp.Body.Close()
	var result struct {
		TargetIP   string `json:"target_ip"`
		TargetPort int    `json:"target_port,omitempty"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		log.Printf("edge: decode daemon response: %v", err)
		return "", 0
	}
	return result.TargetIP, result.TargetPort
}

func pumpUDPToQUIC(ctx context.Context, udp *net.UDPConn, conn *quictransport.Conn, cfg Config, lastApp *atomic.Pointer[net.UDPAddr]) error {
	buf := make([]byte, 65535)
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		n, addr, err := udp.ReadFromUDP(buf)
		if err != nil {
			return err
		}
		if addr != nil {
			a := *addr
			lastApp.Store(&a)
		}
		payload := cfg.Policy.OnEgress(buf[:n])
		if payload == nil {
			continue
		}
		cfg.Metrics.IncBytesSentByTag(payload, len(payload))
		var sendErr error
		switch common.ClassifyPrefix(payload) {
		case common.KindLossyData, common.KindLosslessData:
			sendErr = conn.SendDatagram(payload)
		case common.KindControl:
			sendErr = conn.SendControl(payload)
		default:
			sendErr = conn.SendDatagram(payload)
		}
		if sendErr != nil {
			// On context cancellation the connection is being torn down; exit cleanly.
			if ctx.Err() != nil {
				return ctx.Err()
			}
			log.Printf("edge: send: %v", sendErr)
		}
	}
}

// pumpControlIntoBandwidth reads control frames from the peer and dispatches
// by magic. FBCK frames update the RemoteBackend. Non-FBCK frames are written
// back on the same local UDP socket used for ingress, to the last source
// address seen from the local inference app (unified port).
func pumpControlIntoBandwidth(
	ctx context.Context,
	conn *quictransport.Conn,
	remote *bandwidth.RemoteBackend,
	udp *net.UDPConn,
	lastApp *atomic.Pointer[net.UDPAddr],
	m *metrics.Registry,
) error {
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
		m.IncBytesRecvByTag(msg, len(msg))
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
			// CSTR / RATE / ACTN to local app: reply to last inbound UDP source.
			dst := lastApp.Load()
			if dst == nil {
				continue
			}
			if _, werr := udp.WriteToUDP(msg, dst); werr != nil {
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

// streambed-quic-sidecar: single binary, role chosen by SIDECAR_ROLE env.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/streambed/sidecar/internal/edge"
	"github.com/streambed/sidecar/internal/metrics"
	"github.com/streambed/sidecar/internal/server"
)

func main() {
	role := flag.String("role", env("SIDECAR_ROLE", "edge"), "edge|server")
	daemonURL := flag.String("daemon-url", env("DAEMON_URL", ""), "edge role: base URL of co-located daemon (e.g. http://host:9090)")
	peerQUICPort := flag.String("peer-quic-port", env("PEER_QUIC_PORT", "4433"), "edge role: QUIC port on the server sidecar")
	localUDP := flag.String("local-udp", env("LOCAL_UDP_BIND", "0.0.0.0:9050"), "single UDP bind for app↔sidecar (edge and server roles)")
	bind := flag.String("bind", env("QUIC_BIND", "0.0.0.0:4433"), "QUIC bind (server role)")
	metricsAddr := flag.String("metrics", env("METRICS_ADDR", ":9100"), "Prometheus metrics bind")
	flag.Parse()

	reg := metrics.New()
	go func() {
		mux := http.NewServeMux()
		mux.Handle("/metrics", reg)
		log.Printf("metrics: serving %s/metrics", *metricsAddr)
		if err := http.ListenAndServe(*metricsAddr, mux); err != nil {
			log.Printf("metrics http server: %v", err)
		}
	}()
	go reg.LogLoop(10*time.Second, *role)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	var err error
	switch *role {
	case "edge":
		if *daemonURL == "" {
			log.Fatal("edge role requires DAEMON_URL / -daemon-url")
		}
		peerPort, perr := strconv.Atoi(*peerQUICPort)
		if perr != nil {
			log.Fatalf("invalid PEER_QUIC_PORT %q: %v", *peerQUICPort, perr)
		}
		err = edge.Run(ctx, edge.Config{
			LocalUDPBind: *localUDP,
			DaemonURL:    *daemonURL,
			PeerQUICPort: peerPort,
			Metrics:      reg,
		})
	case "server":
		err = server.Run(ctx, server.Config{
			BindAddr:     *bind,
			LocalUDPBind: *localUDP,
			Metrics:      reg,
		})
	default:
		log.Fatalf("unknown role %q (want edge|server)", *role)
	}
	if err != nil && ctx.Err() == nil {
		log.Fatalf("sidecar exited: %v", err)
	}
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	_ = def
	return def
}

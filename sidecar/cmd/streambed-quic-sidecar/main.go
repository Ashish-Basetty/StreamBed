// streambed-quic-sidecar: single binary, role chosen by SIDECAR_ROLE env.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/streambed/sidecar/internal/edge"
	"github.com/streambed/sidecar/internal/metrics"
	"github.com/streambed/sidecar/internal/server"
)

func main() {
	role := flag.String("role", env("SIDECAR_ROLE", "edge"), "edge|server")
	peer := flag.String("peer", env("PEER_ADDRESS", ""), "peer sidecar address (edge role)")
	localUDP := flag.String("local-udp", env("LOCAL_UDP_BIND", "0.0.0.0:9050"), "local UDP bind (edge role)")
	daemon := flag.String("daemon", env("DAEMON_ADDRESS", "127.0.0.1:9051"), "where to send peer-originated feedback (edge role)")
	bind := flag.String("bind", env("QUIC_BIND", "0.0.0.0:4433"), "QUIC bind (server role)")
	localServer := flag.String("local-server", env("LOCAL_SERVER_UDP", "127.0.0.1:9000"), "server container UDP target (server role)")
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
		if *peer == "" {
			log.Fatal("edge role requires PEER_ADDRESS / -peer")
		}
		err = edge.Run(ctx, edge.Config{
			LocalUDPBind: *localUDP,
			PeerAddr:     *peer,
			DaemonAddr:   *daemon,
			Metrics:      reg,
		})
	case "server":
		err = server.Run(ctx, server.Config{
			BindAddr:           *bind,
			LocalServerUDPAddr: *localServer,
			Metrics:            reg,
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

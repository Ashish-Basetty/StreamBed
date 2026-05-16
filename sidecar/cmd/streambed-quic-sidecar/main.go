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
	"github.com/streambed/sidecar/internal/heartbeat"
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
	controllerURL := flag.String("controller-url", env("CONTROLLER_URL", ""), "controller base URL (heartbeat)")
	deviceCluster := flag.String("device-cluster", env("DEVICE_CLUSTER", ""), "device cluster (heartbeat)")
	deviceID := flag.String("device-id", env("DEVICE_ID", ""), "device id (heartbeat)")
	sidecarHostIP := flag.String("sidecar-host-ip", env("SIDECAR_HOST_IP", ""), "host IP daemon publishes us at (heartbeat)")
	sidecarHostPort := flag.String("sidecar-host-port", env("SIDECAR_HOST_PORT", ""), "host UDP port mapped to our QUIC bind (heartbeat)")
	heartbeatInterval := flag.String("heartbeat-interval", env("HEARTBEAT_INTERVAL", "10s"), "controller heartbeat interval (Go duration)")
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

	hbInterval, hbErr := time.ParseDuration(*heartbeatInterval)
	if hbErr != nil {
		log.Printf("heartbeat: invalid HEARTBEAT_INTERVAL %q (%v); defaulting to 10s", *heartbeatInterval, hbErr)
		hbInterval = 10 * time.Second
	}
	hbHostPort, _ := strconv.Atoi(*sidecarHostPort)
	go heartbeat.Loop(ctx, heartbeat.Config{
		ControllerURL: *controllerURL,
		Cluster:       *deviceCluster,
		DeviceID:      *deviceID,
		Role:          *role,
		HostIP:        *sidecarHostIP,
		HostPort:      hbHostPort,
		DaemonURL:     *daemonURL,
		Interval:      hbInterval,
		Reg:           reg,
	})

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

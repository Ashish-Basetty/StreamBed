// Package heartbeat posts periodic liveness + counter snapshots from the
// sidecar to the controller. Owning the heartbeat here (rather than in the
// inference container) restores the abstraction boundary — the inference
// process never talks to the controller — and lets the controller
// distinguish "process alive" from "data plane actually moving."
package heartbeat

import (
	"bytes"
	"context"
	"encoding/json"
	"log"
	"net/http"
	"time"

	"github.com/streambed/sidecar/internal/metrics"
)

type Config struct {
	ControllerURL string // e.g. "http://controller:8080"
	Cluster       string
	DeviceID      string
	Role          string // "edge" | "server"
	HostIP        string // DAEMON_PUBLIC_IP
	HostPort      int    // SIDECAR_HOST_PORT (host-side UDP port for our QUIC bind)
	DaemonURL     string // co-located daemon base URL; queried for /inference-status
	Interval      time.Duration
	Reg           *metrics.Registry
}

type request struct {
	DeviceCluster   string `json:"device_cluster"`
	DeviceID        string `json:"device_id"`
	Role            string `json:"role"`
	SidecarHostIP   string `json:"sidecar_host_ip"`
	SidecarHostPort int    `json:"sidecar_host_port"`
	DGTotal         uint64 `json:"dg_total"`
	// InferenceAlive piggybacks the daemon's docker-inspect state so the
	// controller can distinguish "process died" from "no upstream traffic".
	// True if the daemon doesn't report otherwise (so transient daemon HTTP
	// failures don't trigger spurious restarts).
	InferenceAlive bool `json:"inference_alive"`
}

// Loop posts every cfg.Interval until ctx is cancelled. Failures are logged
// and ignored — the controller will mark the device unresponsive on its own
// timeout if the loop stays broken.
func Loop(ctx context.Context, cfg Config) {
	if cfg.ControllerURL == "" || cfg.DeviceID == "" {
		log.Printf("heartbeat: disabled (controller_url=%q device_id=%q)", cfg.ControllerURL, cfg.DeviceID)
		return
	}
	if cfg.Interval <= 0 {
		cfg.Interval = 10 * time.Second
	}

	url := cfg.ControllerURL + "/sidecar-heartbeat"
	infStatusURL := ""
	if cfg.DaemonURL != "" {
		infStatusURL = cfg.DaemonURL + "/inference-status"
	}
	client := &http.Client{Timeout: 5 * time.Second}

	post := func() {
		dgTotal := uint64(0)
		if cfg.Reg != nil {
			dgTotal = cfg.Reg.DatagramsSent.Load() + cfg.Reg.DatagramsReceived.Load()
		}
		body := request{
			DeviceCluster:   cfg.Cluster,
			DeviceID:        cfg.DeviceID,
			Role:            cfg.Role,
			SidecarHostIP:   cfg.HostIP,
			SidecarHostPort: cfg.HostPort,
			DGTotal:         dgTotal,
			InferenceAlive:  fetchInferenceAlive(ctx, client, infStatusURL),
		}
		buf, err := json.Marshal(body)
		if err != nil {
			log.Printf("heartbeat: marshal: %v", err)
			return
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(buf))
		if err != nil {
			log.Printf("heartbeat: new request: %v", err)
			return
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			if ctx.Err() == nil {
				log.Printf("heartbeat: post %s: %v", url, err)
			}
			return
		}
		resp.Body.Close()
		if resp.StatusCode >= 300 {
			log.Printf("heartbeat: controller returned %s", resp.Status)
		}
	}

	post()
	ticker := time.NewTicker(cfg.Interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			post()
		}
	}
}

// fetchInferenceAlive asks the co-located daemon if the inference container
// is in running state. On any failure (URL unset, HTTP error, decode error),
// default to true — a transient daemon hiccup should not produce a false
// "dead" signal and trigger a restart loop. The controller treats false as
// load-bearing; true is the safe default.
func fetchInferenceAlive(ctx context.Context, client *http.Client, url string) bool {
	if url == "" {
		return true
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return true
	}
	resp, err := client.Do(req)
	if err != nil {
		if ctx.Err() == nil {
			log.Printf("heartbeat: poll %s: %v", url, err)
		}
		return true
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return true
	}
	var result struct {
		Alive bool `json:"alive"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		log.Printf("heartbeat: decode /inference-status: %v", err)
		return true
	}
	return result.Alive
}

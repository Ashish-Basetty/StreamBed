# GCP Testing Plan

Sibling doc to [GCPTestInfra.md](GCPTestInfra.md). That doc covers *provisioning* the 5-VM cluster. This one covers *running tests on it* — specifically, the tests that local `docker compose` physically cannot run because everything lives in one kernel.

## Scope and non-goals

**In scope:** tests gated by `pytest.mark.gcp` that exercise cross-host networking (real VPC, real internal IPs, real link RTT, real firewall) using the existing 5-VM Terraform layout.

**Not in scope:** replacing the local `docker compose` integration tests. Those stay the canonical fast loop. GCP tests are a small, expensive, run-on-demand suite.

## The networking answer (no overlay, no tunnel)

The daemon already takes its public address from `DAEMON_PUBLIC_IP` ([daemon_config.py:26](../control-plane/DeploymentDaemon/daemon_config.py#L26)) — the comment even calls out GCP. The only thing baked to localhost is the *value supplied by `docker-compose.yml`*, which hardcodes `host.docker.internal`.

So "universal address" already exists as an abstraction. Make it data-driven:

```
local laptop      DAEMON_PUBLIC_IP = host.docker.internal     (current)
GCP per-VM        DAEMON_PUBLIC_IP = <this VM's internal IP>  (from GCE metadata)
```

Each value is supplied by the operator at boot, not by code. No overlay (Tailscale / swarm / weave), no virtual IPs, no host-network mode. The GCE metadata service *is* the discovery mechanism — every VM can read its own internal IP at `http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/ip` without leaving the kernel.

Sidecar QUIC ports are already host-published per daemon ([docker-compose.yml:73-83](../docker-compose.yml#L73)). The same port-publish scheme works across VMs once each daemon knows its host's real IP — the controller just hands out `10.10.0.X:7301` instead of `host.docker.internal:7301`. No code change needed in the controller, router, or sidecar.

## Topology on GCP

```
controller-01            server-01            server-02            edge-01            edge-02
10.10.0.x (reserved)     10.10.0.y            10.10.0.z            10.10.0.a          10.10.0.b
                         daemon-server1       daemon-server2       daemon-edge1       daemon-edge2
controller (8080)        sidecar + inference  sidecar + inference  sidecar + inference sidecar + inference
router (8090)
```

One daemon per VM (vs the local five-on-one-host). Controller and router only on controller-01. Each worker VM runs a one-service compose stack.

## Files to add/change (minimal touch)

1. **`docker-compose.yml`** — one-line change per daemon: `DAEMON_PUBLIC_IP=${DAEMON_PUBLIC_IP:-host.docker.internal}`. Default preserves local behavior exactly. Same for `CONTROLLER_URL`, `DAEMON_PUBLIC_PORT`. No other edits.
2. **`infra/gcp/docker-compose.controller.yml`** *(new)* — controller + router only. Pulls images from registry (no `build:` on the VM).
3. **`infra/gcp/docker-compose.worker.yml`** *(new)* — single `daemon` service. Reads role + IP + controller URL from `/etc/default/streambed` via `env_file:`.
4. **`infra/gcp/startup.sh`** — append three lines that stamp `DAEMON_PUBLIC_IP`, `DAEMON_PUBLIC_PORT`, `SIDECAR_PORT_RANGE_MIN/MAX` into `/etc/default/streambed` from the metadata service.
5. **`infra/gcp/vms.tf`** — add per-worker metadata for `device-id` mapped to the daemon `DEVICE_ID` the controller expects (already there); add `daemon-public-port` and `sidecar-port-range-*` as metadata so the startup script doesn't have to hardcode role→port mapping.
6. **`tests/conftest.py`** — add `--controller-url` CLI option (defaults to `http://localhost:8080`); GCP tests skip the compose-up fixtures when this is overridden. ~10 lines.
7. **`tests/gcp/`** *(new dir)* — `pytest.mark.gcp` tests. See below.

What we are *not* changing: daemon Python, sidecar Go, controller, router, mock_video_server, or the existing local tests.

### Optional daemon nicety (deferred)

The daemon could fall back to GCE metadata if `DAEMON_PUBLIC_IP` is unset — `169.254.169.254` is reachable from inside bridge-networked containers on GCE. This makes the daemon "self-aware" on GCP without env wiring. **Defer**: the startup-script-stamps-env approach is fully explicit and leaves the daemon GCP-agnostic. Revisit only if env wiring becomes annoying.

## Test runner: laptop → controller via IAP

Run `pytest -m gcp` from the laptop. Two access options for the controller's port 8080:

- **Home-IP firewall (current)**: `allow_controller_http` already opens 8080 to `var.your_home_ip_cidr` ([main.tf:56-67](../infra/gcp/main.tf#L56)). Tests point at `http://<controller-external-ip>:8080`. Works from your home network only.
- **IAP tunnel (recommended)**: `gcloud compute start-iap-tunnel controller-01 8080 --local-host-port=localhost:8080`. Tests point at `http://localhost:8080`, same URL as the local suite. Works from anywhere with `gcloud auth`. No firewall change needed.

Prefer IAP — same URL as local means the only thing different is "is the tunnel up?".

## Mock video source on GCP

The local `mock_video_server` runs on the laptop; edges reach it via `host.docker.internal`. On GCP, the laptop is not reachable from edge VMs. Simplest path: **bake `mock_video_server.py` into a tiny side-container on each edge VM** (or as a host-side systemd unit), accessible at `127.0.0.1:9200`. The daemon's `VIDEO_SERVER_HOST` env var already exists ([daemon_config.py:59](../control-plane/DeploymentDaemon/daemon_config.py#L59)).

Alternative: run one mock-video instance on the controller VM and point all edges at its internal IP. Slightly more realistic (real network hop), trivially more setup.

## Test list (stubs first, fill in incrementally)

Each marked `@pytest.mark.gcp`. None of these should run as part of the default `pytest` invocation.

1. `test_cross_vm_registration.py` — controller sees 4 worker VMs in `/devices`, each with its 10.10.0.X internal IP, not `host.docker.internal`.
2. `test_cross_vm_quic_handshake.py` — deploy edge on edge-01, server on server-01; assert sidecar handshake succeeds across the VPC.
3. `test_actn_roundtrip_latency.py` — measure end-to-end ACTN RTT on real link. Sanity bound (e.g. <10ms within the zone). Mostly diagnostic, not pass/fail.
4. `test_firewall_negative.py` — assert ports 8090 (router), 9090-9094 (daemons), 7001-7500 (sidecar UDP) are *not* reachable from the laptop's external network. Catches accidental firewall opens.
5. `test_startup_script.py` — SSH into each VM, assert `/etc/default/streambed` has all expected keys and a non-empty `DAEMON_PUBLIC_IP`.

## Cost

Per [GCPTestInfra.md](GCPTestInfra.md#realistic-budget-under-this-plan): burst-only at ~$7/mo, plenty of margin against the $50 credit. Adding GCP tests does not change the cost model — VMs are already provisioned. The discipline is `./vms.sh stop` after a test session.

## Order of work (small steps)

1. Land the `${VAR:-default}` interpolation in `docker-compose.yml`. Local tests still pass — proves backwards compat.
2. Add `tests/conftest.py` `--controller-url` flag. Local tests still pass.
3. Push controller, router, daemon images to the registry the sidecar already uses (`ashishbasetty/streambed-*`). No infra touched yet.
4. Write `infra/gcp/docker-compose.{controller,worker}.yml` + the startup-script additions.
5. `terraform apply` (re-applies idempotently; VMs may need restart to re-run startup script — easier to `terraform taint` the worker instances).
6. SSH controller, `docker compose -f docker-compose.controller.yml up -d`. Smoke: hit `http://<ext-ip>:8080/devices`, no workers yet.
7. SSH each worker, `docker compose -f docker-compose.worker.yml up -d`. Smoke: same endpoint now shows 4 workers with internal IPs.
8. Implement test #1 (registration). Iterate.
9. Implement tests #2–5.

## Open risks / decisions deferred

- **Image registry auth on VMs.** Public Docker Hub pulls work without creds. If images go private later, VMs need a registry credential (Artifact Registry + GCE service account is the clean path).
- **VM restart needed to pick up new startup-script values.** GCE only runs startup-script on boot. `gcloud compute instances reset` is fine for our cadence; bake this into `vms.sh` if it becomes routine.
- **Sidecar UDP MTU on GCP.** Default MTU in the VPC is 1460; if QUIC paths assume 1500, expect fragmentation. Not certain this matters — flagged for the latency test to surface.
- **No CI integration.** Tests are manual `pytest -m gcp --controller-url=http://localhost:8080` after starting the IAP tunnel. Acceptable for capstone scope.

# StreamBed

Final Project for CS 214: Big Data Systems. Distributed computation for vision-based models.

## Repository layout & testing

- `edge/` – edge device code (video capture, local inference, streaming, API).
- `server/` – server-side inference containers.
- `controller/` – orchestration, model deployment, routing, and heartbeats.
- `shared/` – common utilities (streaming protocol, storage, inference, APIs).
- `tests/` – pytest test suite (unit and integration).

## Testing

Install pytest, then run tests:

```sh
pip install pytest
pytest tests/ -v -s
```

### Test markers

Tests are tagged with pytest markers. Run specific suites:

| Command | Description |
|---------|-------------|
| `pytest tests/ -v -s -m unit` | Unit tests only (fast, no external services) |
| `pytest tests/ -v -s -m integration_stream` | Stream/network integration tests |
| `pytest tests/ -v -s -m integration_docker` | Docker integration: failure detection, deploy/delete (requires Docker) |
| `pytest tests/ -v -s -m "integration and not integration_docker"` | Integration tests without Docker |

### Test layout

- **`tests/unit/`** – Unit tests: frame store, TTL manager, inference, stream interface, network simulation, retrieval API.
- **`tests/test_controller_rerouting.py`** – Integration: edge failure and rerouting to another server.
- **`tests/test_integration_stream_to_storage.py`** – Integration: UDP stream → frame store.
- **`tests/test_failure_detection_docker.py`** – Integration: failure detection with Docker and docker-compose.
- **`tests/test_deployment.py`** – Integration: deploy and delete via controller API (Docker).

### Run via script

```sh
python tests/run_all_tests.py              # Run all tests
python tests/run_all_tests.py unit         # Unit tests only
python tests/run_all_tests.py integration  # Integration (excl. Docker)
python tests/run_all_tests.py docker       # Docker integration tests
```

## QUIC transport

Edge → server frame flow rides on a per-device Go QUIC sidecar
([sidecar/](sidecar/)). The daemon spawns one sidecar container next to each
inference container; the controller tells the edge sidecar where the server
sidecar listens. The inference container speaks plain UDP locally to its own
sidecar (`SIDECAR_HOST:SIDECAR_UDP_PORT`) and the sidecar carries the bytes
across the host boundary over QUIC.

Start here:

- [docs/QuicSidecarPrimer.md](docs/QuicSidecarPrimer.md) — walkthrough of
  every file in `sidecar/`, for someone who hasn't written Go.
- [docs/quic-go-plan.md](docs/quic-go-plan.md) — the original design / why-we-built-this.
- [docs/QuicDynamicRoutingPlan.md](docs/QuicDynamicRoutingPlan.md) — how the
  controller hot-swaps an edge's target server at runtime.
- [docs/RoutingTableAndSidecars.md](docs/RoutingTableAndSidecars.md) — how
  the sidecar's host endpoint gets published into the routing table.

## GCP cluster tests

A second test suite (`pytest -m gcp tests/gcp/`) targets a real 5-VM GCP
cluster — one controller, two servers, two edges, single VPC. Used to catch
cross-host networking + QUIC bugs that `docker compose` on one machine can't
reproduce. Tests are marked `pytest.mark.gcp` and **skipped unless the
controller is reachable**, so they never run by accident.

Full details:

- [tests/gcp/README.md](tests/gcp/README.md) — running the GCP test suite.
- [infra/gcp/README.md](infra/gcp/README.md) — Terraform layout, costs, and
  one-time setup (gcloud auth, project, budget alert, `terraform apply`).
- [docs/finished/GCPTestInfra.md](docs/finished/GCPTestInfra.md) —
  background: why these specific 5 VMs, why us-central1-a, budget math.

### Quickstart (cluster already provisioned)

> The controller + worker compose stacks use `restart: unless-stopped` and
> Docker autostarts on boot, so steps 2 and 3 are **one-time per VM**. On
> subsequent sessions `vms.sh start` is enough — Docker brings the stacks
> back up automatically. Re-run the compose commands only after a fresh
> `terraform apply` or an explicit `docker compose down` on the VM.

```sh
# 1. Boot the VMs (each ~$0.30/hr while running; stop them when you're done).
cd infra/gcp && ./vms.sh start

# 2. (First session only.) Bring the controller stack up.
gcloud compute ssh controller-01 --zone=us-central1-a --tunnel-through-iap \
  --command='cd ~/StreamBed && sudo docker compose -f infra/gcp/docker-compose.controller.yml up -d'

# 3. (First session only, per worker.) Bring the daemon stack up.
for vm in server-01 server-02 edge-01 edge-02; do
  gcloud compute ssh "$vm" --zone=us-central1-a --tunnel-through-iap \
    --command='cd ~/StreamBed && sudo docker compose -f infra/gcp/docker-compose.worker.yml up -d'
done

# 4. Open the IAP tunnel. Bound to :18080 (not :8080) so it doesn't collide
#    with a local docker-compose controller. Leave running in a separate
#    terminal. "Bad file descriptor" lines from probes that hang up early
#    are cosmetic — ignore as long as `curl http://localhost:18080/health`
#    returns `{"status":"ok"}`.
gcloud compute start-iap-tunnel controller-01 8080 \
  --zone=us-central1-a --local-host-port=localhost:18080

# 5. Run the suite. The --controller-url default in tests/conftest.py is
#    already http://localhost:18080, so no flag needed.
pytest -m gcp tests/gcp/ -v

# 6. When done — single biggest cost lever.
cd infra/gcp && ./vms.sh stop
```

`vms.sh` reads VM names and the zone from `terraform output`, so it always
operates on whatever's currently provisioned:

| Command | Effect |
|---|---|
| `./vms.sh start` | Boot every VM in the cluster. |
| `./vms.sh stop` | Power off every VM. State is preserved; you only pay ~$2/mo for disk. |
| `./vms.sh status` | Show name / running state / machine type / internal IP for each VM. |

If `--controller-url` isn't passed, the GCP tests assume `http://localhost:18080`
(the IAP tunnel target). To point at the controller's external IP directly:

```sh
pytest -m gcp tests/gcp/ --controller-url=http://<external-ip>:8080
```

### Adding a new GCP test

The fixtures live in [tests/gcp/conftest.py](tests/gcp/conftest.py) and
[tests/gcp/deploy_helpers.py](tests/gcp/deploy_helpers.py).
`deployed_edge_server_pair(url, cluster)` is the usual entry point — it
deploys `server-01` + `edge-01` with mock-video wired in, yields, and tears
down on exit. Mark the test `@pytest.mark.gcp` and depend on the
`gcp_controller_reachable` fixture so it auto-skips when the tunnel is down.

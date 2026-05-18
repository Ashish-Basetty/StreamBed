# GCP Testing — Resume Here

Scratch doc for picking up where we left off. Delete once we have a green
`pytest -m gcp` run.

## State as of last session

**Cluster** (`./infra/gcp/vms.sh status` to verify):
- 5 VMs exist: `controller-01`, `edge-01`, `edge-02`, `server-01`, `server-02`.
- All STOPPED.
- Subnet `streambed-subnet` now has Private Google Access enabled (mirrors
  the [main.tf change](../infra/gcp/main.tf)).
- Workers now have ephemeral external IPs attached (mirrors the
  [vms.tf change](../infra/gcp/vms.tf)). They had none originally; we found
  out the hard way that `us-central1.gce.archive.ubuntu.com` does NOT work
  over PGA, so workers needed external egress to install `docker.io` via
  apt. External IPs are free while attached to a running VM. Ingress is
  still blocked — no firewall rule lets the public reach a worker.

**Per-VM bootstrap state** (Docker installed + `/etc/default/streambed`
written):
- `controller-01` — DONE
- `edge-01`, `edge-02`, `server-01` — DONE
- `server-02` — **NOT DONE.** Lost the dpkg lock to `unattended-upgrades`
  on the last attempt. One ssh retry will fix it.

**Scaffolded but not yet exercised**:
- [infra/gcp/docker-compose.controller.yml](../infra/gcp/docker-compose.controller.yml)
- [infra/gcp/docker-compose.worker.yml](../infra/gcp/docker-compose.worker.yml)
- [tests/gcp/](../tests/gcp/) (5 tests, 2 implemented, 3 stub)

## Resume checklist

### 1. Start the cluster

```
cd infra/gcp
./vms.sh start
```

Wait ~30s for sshd to come up. Workers get NEW ephemeral external IPs (the
old ones were released on stop — fine, they're only used for apt).

### 2. Finish server-02 bootstrap

The bootstrap script lives at `/tmp/streambed_bootstrap.sh` on your
laptop (or regenerate from the snippet at the bottom of this doc).
**`/tmp` on the VMs is tmpfs — wiped on every stop/start**, so you must
re-scp before running:

```
gcloud compute scp /tmp/streambed_bootstrap.sh server-02:/tmp/ \
  --zone=us-central1-a --tunnel-through-iap \
&& gcloud compute ssh server-02 --zone=us-central1-a --tunnel-through-iap \
   --command='while sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do sleep 5; done; sudo bash /tmp/streambed_bootstrap.sh'
```

The `while sudo fuser` loop waits out `unattended-upgrades`, which holds
the dpkg lock for ~minutes after first boot.

### 3. Push images to Docker Hub (linux/amd64)

You haven't done this yet. Workers + controller pull from there.

```
docker buildx build --platform linux/amd64 -t ashishbasetty/streambed-controller:latest \
  -f control-plane/ControllerNode/Dockerfile --push .
docker buildx build --platform linux/amd64 -t ashishbasetty/streambed-router:latest \
  -f control-plane/Router/Dockerfile --push .
docker buildx build --platform linux/amd64 -t ashishbasetty/streambed-daemon:latest \
  -f control-plane/DeploymentDaemon/Dockerfile --push .
```

(The sidecar image is already pushed — that one's been the dev image for a
while.)

### 4. scp compose files to each VM

```
# controller
gcloud compute scp infra/gcp/docker-compose.controller.yml \
  controller-01:~/docker-compose.yml --zone=us-central1-a --tunnel-through-iap

# workers
for vm in edge-01 edge-02 server-01 server-02; do
  gcloud compute scp infra/gcp/docker-compose.worker.yml \
    $vm:~/docker-compose.yml --zone=us-central1-a --tunnel-through-iap
done
```

### 5. Bring up the stack

**Note:** workers use `docker-compose` (v1, hyphenated) — apt's Ubuntu
22.04 archive doesn't carry the v2 plugin. Use `docker-compose -f ...`
not `docker compose -f ...`.

```
# controller
gcloud compute ssh controller-01 --zone=us-central1-a --tunnel-through-iap \
  --command='sudo docker-compose -f ~/docker-compose.yml up -d'

# each worker
for vm in edge-01 edge-02 server-01 server-02; do
  gcloud compute ssh $vm --zone=us-central1-a --tunnel-through-iap \
    --command='sudo docker-compose -f ~/docker-compose.yml up -d'
done
```

### 6. Open IAP tunnel + run tests

```
# in one terminal
gcloud compute start-iap-tunnel controller-01 8080 \
  --zone=us-central1-a --local-host-port=localhost:8080

# in another
pytest -m gcp tests/gcp/
```

### 7. Stop when done

```
./vms.sh stop
```

## Gotchas to remember

- **Ubuntu's `docker-compose` is v1 (hyphenated).** Worker compose syntax
  is v3.x spec but v1 handles it. Don't reach for `docker compose` on the
  VMs.
- **Workers' external IPs change on every start.** Doesn't matter for our
  setup — we only used them for `apt`, and they're not in any config file.
  Internal IPs (10.10.0.x) ARE sticky across stop/start.
- **`startup.sh` re-runs on every boot.** If you edit it, the next start
  will pick it up. To force a re-run without restarting, ssh in and
  `sudo bash /tmp/streambed_bootstrap.sh` (or call the script directly).
- **Don't `terraform apply` without reading the diff first.** Your live
  cluster matches the current tf state, but the apply MIGHT decide to
  recreate workers if it dislikes the access_config diff format. Read
  `terraform plan` carefully before saying yes.

## Bootstrap script snippet (regenerate if needed)

```bash
cat > /tmp/streambed_bootstrap.sh <<'EOF'
#!/bin/bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y docker.io docker-compose git curl
systemctl enable --now docker

META="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
HDR="Metadata-Flavor: Google"
DEVICE_ID=$(curl -fsS -H "$HDR" "$META/device-id")
DEVICE_TYPE=$(curl -fsS -H "$HDR" "$META/device-type")
DEVICE_CLUSTER=$(curl -fsS -H "$HDR" "$META/device-cluster")
CONTROLLER_URL=$(curl -fsS -H "$HDR" "$META/controller-url")
DAEMON_PUBLIC_IP=$(curl -fsS -H "$HDR" \
  "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/ip")
DAEMON_PUBLIC_PORT=$(curl -fsS -H "$HDR" "$META/daemon-public-port" 2>/dev/null || echo 9090)
SIDECAR_PORT_RANGE_MIN=$(curl -fsS -H "$HDR" "$META/sidecar-port-range-min" 2>/dev/null || echo 7001)
SIDECAR_PORT_RANGE_MAX=$(curl -fsS -H "$HDR" "$META/sidecar-port-range-max" 2>/dev/null || echo 7100)

cat <<E2 > /etc/default/streambed
DEVICE_ID=${DEVICE_ID}
DEVICE_TYPE=${DEVICE_TYPE}
DEVICE_CLUSTER=${DEVICE_CLUSTER}
CONTROLLER_URL=${CONTROLLER_URL}
DAEMON_PUBLIC_IP=${DAEMON_PUBLIC_IP}
DAEMON_PUBLIC_PORT=${DAEMON_PUBLIC_PORT}
SIDECAR_PORT_RANGE_MIN=${SIDECAR_PORT_RANGE_MIN}
SIDECAR_PORT_RANGE_MAX=${SIDECAR_PORT_RANGE_MAX}
DEVICE_NETWORK_NAME=streambed-device-net
E2
chmod 0644 /etc/default/streambed
echo "bootstrap done: ${DEVICE_ID} ip=${DAEMON_PUBLIC_IP}"
EOF
```

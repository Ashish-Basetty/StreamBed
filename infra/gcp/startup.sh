#!/bin/bash
# StreamBed VM bootstrap. Runs once on first boot; idempotent on reruns.
# Installs Docker and writes GCE metadata into /etc/default/streambed so
# docker compose can pick the values up.

set -euxo pipefail

# --- Docker (engine + legacy compose v1) ---
# Worker VMs are internal-only (no external IP, no Cloud NAT), so we can't
# reach get.docker.com or download.docker.com. The Ubuntu archive served
# from us-central1.gce.archive.ubuntu.com is reachable from inside the VPC
# and carries `docker.io` + the legacy Python `docker-compose` (v1.29). Use
# `docker-compose -f ...` (hyphen) instead of `docker compose -f ...`.
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io docker-compose git curl
systemctl enable --now docker

# --- Pull StreamBed metadata into /etc/default/streambed ---
META="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
HDR="Metadata-Flavor: Google"

DEVICE_ID=$(curl -fsS -H "$HDR" "$META/device-id")
DEVICE_TYPE=$(curl -fsS -H "$HDR" "$META/device-type")
DEVICE_CLUSTER=$(curl -fsS -H "$HDR" "$META/device-cluster")
CONTROLLER_URL=$(curl -fsS -H "$HDR" "$META/controller-url")

# DAEMON_PUBLIC_IP: this VM's primary internal IP. Other VMs (controller,
# peer daemons) use it to reach this VM's daemon HTTP API and the sidecar's
# host-published QUIC port. Read from GCE network-interfaces metadata so it
# survives stop/start (internal IP is sticky to the NIC).
DAEMON_PUBLIC_IP=$(curl -fsS -H "$HDR" \
  "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/ip")

# Defaults for the per-VM compose. Controller doesn't read these but it's
# harmless to stamp them, and skipping the metadata lookup for workers vs
# controller would be more code than just always writing the file.
DAEMON_PUBLIC_PORT=$(curl -fsS -H "$HDR" "$META/daemon-public-port" || echo 9090)
SIDECAR_PORT_RANGE_MIN=$(curl -fsS -H "$HDR" "$META/sidecar-port-range-min" || echo 7001)
SIDECAR_PORT_RANGE_MAX=$(curl -fsS -H "$HDR" "$META/sidecar-port-range-max" || echo 7100)

cat <<EOF > /etc/default/streambed
DEVICE_ID=${DEVICE_ID}
DEVICE_TYPE=${DEVICE_TYPE}
DEVICE_CLUSTER=${DEVICE_CLUSTER}
CONTROLLER_URL=${CONTROLLER_URL}
DAEMON_PUBLIC_IP=${DAEMON_PUBLIC_IP}
DAEMON_PUBLIC_PORT=${DAEMON_PUBLIC_PORT}
SIDECAR_PORT_RANGE_MIN=${SIDECAR_PORT_RANGE_MIN}
SIDECAR_PORT_RANGE_MAX=${SIDECAR_PORT_RANGE_MAX}
# Single per-VM device bridge; daemon attaches its sidecar + inference here.
DEVICE_NETWORK_NAME=streambed-device-net
EOF
chmod 0644 /etc/default/streambed

echo "StreamBed bootstrap done: device_id=${DEVICE_ID} type=${DEVICE_TYPE} cluster=${DEVICE_CLUSTER} ip=${DAEMON_PUBLIC_IP}"

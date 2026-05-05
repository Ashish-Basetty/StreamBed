#!/bin/bash
# StreamBed VM bootstrap. Runs once on first boot; idempotent on reruns.
# Installs Docker and writes GCE metadata into /etc/default/streambed so
# docker compose can pick the values up.

set -euxo pipefail

# --- Docker ---
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io docker-compose-plugin git curl
systemctl enable --now docker

# --- Pull StreamBed metadata into /etc/default/streambed ---
META="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
HDR="Metadata-Flavor: Google"

DEVICE_ID=$(curl -fsS -H "$HDR" "$META/device-id")
DEVICE_TYPE=$(curl -fsS -H "$HDR" "$META/device-type")
DEVICE_CLUSTER=$(curl -fsS -H "$HDR" "$META/device-cluster")
CONTROLLER_URL=$(curl -fsS -H "$HDR" "$META/controller-url")

cat <<EOF > /etc/default/streambed
DEVICE_ID=${DEVICE_ID}
DEVICE_TYPE=${DEVICE_TYPE}
DEVICE_CLUSTER=${DEVICE_CLUSTER}
CONTROLLER_URL=${CONTROLLER_URL}
EOF
chmod 0644 /etc/default/streambed

echo "StreamBed bootstrap done: device_id=${DEVICE_ID} type=${DEVICE_TYPE} cluster=${DEVICE_CLUSTER}"

#!/usr/bin/env bash
# Deploy the advisor inference containers via the controller's /deploy endpoint.
# Assumes the StreamBed compose stack is already up (controller + daemons +
# sidecars) and the advisor images have been built and pushed.
#
# Usage:
#   bash experiments/advisor/scripts/deploy_advisor.sh
#
# Override images / ports via env:
#   ADVISOR_EDGE_IMAGE=...   ADVISOR_SERVER_IMAGE=...
#   EDGE_HOST_PORT=9100      EDGE_CONTAINER_PORT=9100
#   SERVER_HOST_PORT=8001    SERVER_CONTAINER_PORT=9000
set -euo pipefail

CONTROLLER="${CONTROLLER_URL:-http://localhost:8080}"
ADVISOR_EDGE_IMAGE="${ADVISOR_EDGE_IMAGE:-ashishbasetty/streambed-advisor-edge:latest}"
ADVISOR_SERVER_IMAGE="${ADVISOR_SERVER_IMAGE:-ashishbasetty/streambed-advisor-server:latest}"
EDGE_HOST_PORT="${EDGE_HOST_PORT:-9100}"
EDGE_CONTAINER_PORT="${EDGE_CONTAINER_PORT:-9100}"
SERVER_HOST_PORT="${SERVER_HOST_PORT:-8001}"
SERVER_CONTAINER_PORT="${SERVER_CONTAINER_PORT:-9000}"

# Server first — feature listener must be up before edge starts streaming.
echo "[advisor] deploying advisor-server -> ${ADVISOR_SERVER_IMAGE}"
curl --fail --silent --show-error -X POST "${CONTROLLER}/deploy" \
  -H 'Content-Type: application/json' \
  -d "{
    \"device_cluster\": \"default\",
    \"device_id\": \"server-001\",
    \"device_type\": \"server\",
    \"image\": \"${ADVISOR_SERVER_IMAGE}\",
    \"host_port\": ${SERVER_HOST_PORT},
    \"container_port\": ${SERVER_CONTAINER_PORT}
  }"
echo

echo "[advisor] waiting 5s for advisor-server + sidecar handshake"
sleep 5

echo "[advisor] deploying advisor-edge -> ${ADVISOR_EDGE_IMAGE}"
curl --fail --silent --show-error -X POST "${CONTROLLER}/deploy" \
  -H 'Content-Type: application/json' \
  -d "{
    \"device_cluster\": \"default\",
    \"device_id\": \"edge-001\",
    \"device_type\": \"edge\",
    \"image\": \"${ADVISOR_EDGE_IMAGE}\",
    \"host_port\": ${EDGE_HOST_PORT},
    \"container_port\": ${EDGE_CONTAINER_PORT}
  }"
echo

echo "[advisor] both deployments dispatched. Run frame_gen.py against"
echo "          127.0.0.1:${EDGE_HOST_PORT} once edge container is healthy."

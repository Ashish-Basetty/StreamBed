#!/bin/bash
# Deploy server-001 and edge-001 via controller API.
# Requires controller + daemons running (e.g. docker compose up).
# Run from project root: ./tests/deploy_devices.sh

CONTROLLER_URL="${CONTROLLER_URL:-http://localhost:8080}"
EDGE_DAEMON_URL="${EDGE_DAEMON_URL:-http://localhost:9090}"

echo "[deploy] Deploying server-001..."
curl -s -X POST "${CONTROLLER_URL}/deploy" \
  -H "Content-Type: application/json" \
  -d '{
    "device_cluster": "default",
    "device_id": "server-001",
    "image": "ashishbasetty/streambed-server:latest",
    "host_port": 8001,
    "container_port": 8001
  }'
echo ""

echo "[deploy] Waiting 10s for server to start..."
sleep 10

echo "[deploy] Deploying edge-001..."
curl -s -X POST "${CONTROLLER_URL}/deploy" \
  -H "Content-Type: application/json" \
  -d '{
    "device_cluster": "default",
    "device_id": "edge-001",
    "image": "ashishbasetty/streambed-edge:latest",
    "host_port": 8000,
    "container_port": 8000
  }'
echo ""

echo "[deploy] Done. Optional: point stream-target at throttle proxy:"
echo "  curl -X PUT ${EDGE_DAEMON_URL}/stream-target -H 'Content-Type: application/json' -d '{\"target_ip\":\"throttle-proxy\",\"target_port\":9010}'"

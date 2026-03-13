#!/bin/bash
# Deploy test: register daemon, then send deploy request to controller.
# Run from project root: ./deployment_testing/run_deploy_test.sh
# Requires: docker compose up (or use run_full_test.sh to start + test)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONTROLLER_URL="${CONTROLLER_URL:-http://localhost:8080}"
EDGE_IMAGE="${EDGE_IMAGE:-ashishbasetty/streambed-edge:latest}"
SERVER_IMAGE="${SERVER_IMAGE:-ashishbasetty/streambed-server:latest}"
DEVICE_CLUSTER="${DEVICE_CLUSTER:-default}"

echo "=== Deployment Test ==="
echo "Controller: $CONTROLLER_URL"
echo "Images: edge=$EDGE_IMAGE, server=$SERVER_IMAGE"
echo ""

# Wait for controller to be ready
echo "Waiting for controller..."
for i in {1..30}; do
  if curl -s "$CONTROLLER_URL/health" >/dev/null 2>&1; then
    echo "Controller ready."
    break
  fi
  sleep 1
  if [ $i -eq 30 ]; then
    echo "Controller not ready. Start with: cd deployment_testing && docker compose up -d"
    exit 1
  fi
done

# Wait for daemons to be ready (controller reaches them by hostname, we check via host ports)
echo ""
echo "Waiting for daemons..."
for i in {1..30}; do
  if curl -s "http://localhost:9090/health" >/dev/null 2>&1 && curl -s "http://localhost:9091/health" >/dev/null 2>&1; then
    echo "Daemons ready."
    break
  fi
  sleep 1
  if [ $i -eq 30 ]; then
    echo "Daemons not ready. Ensure: cd deployment_testing && docker compose up -d"
    exit 1
  fi
done

# Deploy server
echo "Deploying server to daemon-server..."
RESP_SERVER=$(curl -s -X POST "$CONTROLLER_URL/deploy" \
  -H "Content-Type: application/json" \
  -d "{
    \"device_cluster\": \"$DEVICE_CLUSTER\",
    \"device_id\": \"server-001\",
    \"image\": \"$SERVER_IMAGE\",
    \"host_port\": 8083,
    \"container_port\": 8001
  }")
echo "$RESP_SERVER" | jq . 2>/dev/null || echo "$RESP_SERVER"
echo ""

# Deploy edge
echo "Deploying edge to daemon-edge..."
RESP_EDGE=$(curl -s -X POST "$CONTROLLER_URL/deploy" \
  -H "Content-Type: application/json" \
  -d "{
    \"device_cluster\": \"$DEVICE_CLUSTER\",
    \"device_id\": \"edge-001\",
    \"image\": \"$EDGE_IMAGE\",
    \"host_port\": 8082,
    \"container_port\": 8000
  }")
echo "$RESP_EDGE" | jq . 2>/dev/null || echo "$RESP_EDGE"
echo ""

if echo "$RESP_SERVER" | grep -q '"ok":true' && echo "$RESP_EDGE" | grep -q '"ok":true'; then
  echo "=== Deploy succeeded ==="
  exit 0
else
  echo "=== Deploy failed ==="
  echo "Server error: $(echo "$RESP_SERVER" | jq -r '.detail // .error // .' 2>/dev/null)"
  echo "Edge error: $(echo "$RESP_EDGE" | jq -r '.detail // .error // .' 2>/dev/null)"
  echo "Check daemon logs: docker logs deploy-test-daemon-server && docker logs deploy-test-daemon-edge"
  exit 1
fi

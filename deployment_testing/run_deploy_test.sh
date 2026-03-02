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

# 1. Register both daemons (use hostnames so controller can reach them)
echo ""
echo "1. Registering daemon-edge with controller..."
curl -s -X POST "$CONTROLLER_URL/register" \
  -H "Content-Type: application/json" \
  -d "{
    \"device_cluster\": \"$DEVICE_CLUSTER\",
    \"device_id\": \"daemon-edge\",
    \"device_type\": \"daemon\",
    \"current_model_version\": \"test\",
    \"ip\": \"deploymentdaemon-edge\"
  }" | jq . 2>/dev/null || cat
echo ""

echo "2. Registering daemon-server with controller..."
curl -s -X POST "$CONTROLLER_URL/register" \
  -H "Content-Type: application/json" \
  -d "{
    \"device_cluster\": \"$DEVICE_CLUSTER\",
    \"device_id\": \"daemon-server\",
    \"device_type\": \"daemon\",
    \"current_model_version\": \"test\",
    \"ip\": \"deploymentdaemon-server\"
  }" | jq . 2>/dev/null || cat
echo ""

# 3. Deploy server
echo "3. Deploying server to daemon-server..."
RESP_SERVER=$(curl -s -X POST "$CONTROLLER_URL/deploy" \
  -H "Content-Type: application/json" \
  -d "{
    \"device_cluster\": \"$DEVICE_CLUSTER\",
    \"device_id\": \"daemon-server\",
    \"image\": \"$SERVER_IMAGE\",
    \"host_port\": 8083,
    \"container_port\": 8001
  }")
echo "$RESP_SERVER" | jq . 2>/dev/null || echo "$RESP_SERVER"
echo ""

# 4. Deploy edge
echo "4. Deploying edge to daemon-edge..."
RESP_EDGE=$(curl -s -X POST "$CONTROLLER_URL/deploy" \
  -H "Content-Type: application/json" \
  -d "{
    \"device_cluster\": \"$DEVICE_CLUSTER\",
    \"device_id\": \"daemon-edge\",
    \"image\": \"$EDGE_IMAGE\",
    \"host_port\": 8082,
    \"container_port\": 8000
  }")
echo "$RESP_EDGE" | jq . 2>/dev/null || echo "$RESP_EDGE"
echo ""

if echo "$RESP_SERVER" | grep -q '"ok":true' && echo "$RESP_EDGE" | grep -q '"ok":true'; then
  echo "=== Deploy succeeded ==="
  echo "Server API: http://localhost:8083/api/v1/health"
  echo "Edge API: http://localhost:8082/api/v1/health"
  exit 0
else
  echo "=== Deploy failed ==="
  exit 1
fi

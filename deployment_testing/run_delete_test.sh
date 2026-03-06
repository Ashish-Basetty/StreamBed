#!/bin/bash
# Delete test: send delete request to controller to stop deployed containers.
# Run from project root: ./deployment_testing/run_delete_test.sh
# Requires: containers already deployed (run run_deploy_test.sh first)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONTROLLER_URL="${CONTROLLER_URL:-http://localhost:8080}"
DEVICE_CLUSTER="${DEVICE_CLUSTER:-default}"

echo "=== Delete Test ==="
echo "Controller: $CONTROLLER_URL"
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

# Delete server-001
echo ""
echo "Deleting containers on server-001..."
RESP_SERVER=$(curl -s -X DELETE "$CONTROLLER_URL/delete" \
  -H "Content-Type: application/json" \
  -d "{
    \"device_cluster\": \"$DEVICE_CLUSTER\",
    \"device_id\": \"server-001\"
  }")
echo "$RESP_SERVER" | jq . 2>/dev/null || echo "$RESP_SERVER"
echo ""

# Delete edge-001
echo "Deleting containers on edge-001..."
RESP_EDGE=$(curl -s -X DELETE "$CONTROLLER_URL/delete" \
  -H "Content-Type: application/json" \
  -d "{
    \"device_cluster\": \"$DEVICE_CLUSTER\",
    \"device_id\": \"edge-001\"
  }")
echo "$RESP_EDGE" | jq . 2>/dev/null || echo "$RESP_EDGE"
echo ""

if echo "$RESP_SERVER" | grep -q '"ok":true' && echo "$RESP_EDGE" | grep -q '"ok":true'; then
  echo "=== Delete succeeded ==="
  exit 0
else
  echo "=== Delete failed ==="
  echo "Server error: $(echo "$RESP_SERVER" | jq -r '.detail // .error // .' 2>/dev/null)"
  echo "Edge error: $(echo "$RESP_EDGE" | jq -r '.detail // .error // .' 2>/dev/null)"
  echo "Check daemon logs: docker logs deploy-test-daemon-server && docker logs deploy-test-daemon-edge"
  exit 1
fi

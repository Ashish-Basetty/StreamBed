#!/bin/bash
# Debug why no frames reach the server.
# Run from project root: ./tests/debug_streaming.sh

set -e

echo "=== 1. Containers running? ==="
docker ps --format '{{.Names}}' | grep -E 'streambed-default-(edge|server)' || echo "(none found)"

echo ""
echo "=== 2. Stream target (edge daemon) ==="
curl -s http://localhost:9090/stream-target 2>/dev/null || echo "daemon-edge1 not reachable"

echo ""
echo "=== 3. Edge logs (last 15 lines) ==="
EDGE=$(docker ps -q -f name=streambed-default-edge 2>/dev/null | head -1)
if [ -n "$EDGE" ]; then
  docker logs "$EDGE" 2>&1 | tail -15
else
  echo "No edge container found"
fi

echo ""
echo "=== 4. Daemon-edge1 logs (stream proxy) ==="
docker logs streambed-daemon-edge1 2>&1 | tail -20

echo ""
echo "=== 5. Server logs (last 15 lines) ==="
SERVER=$(docker ps -q -f name=streambed-default-server 2>/dev/null | head -1)
if [ -n "$SERVER" ]; then
  docker logs "$SERVER" 2>&1 | tail -15
else
  echo "No server container found"
fi

echo ""
echo "=== 6. Server health ==="
curl -s http://localhost:8001/api/v1/health 2>/dev/null || echo "Server API not reachable"

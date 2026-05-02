"""
Integration test: verify the routing table is populated correctly after the
default deployment stack comes up.

Default setup (from deploy_utils.DEVICES / docker-compose.yml):
  Cluster: "default"
  Servers: server-001, server-002
  Edges:   edge-001, edge-002, edge-003

Expected behavior after all daemons register:
  - Every edge has exactly one routing entry (GET /routing returns 3 rows)
  - Every entry targets a valid server (server-001 or server-002)
  - Load is balanced by least-connections: with 3 edges and 2 servers,
    one server gets 2 edges and the other gets 1 (no server gets 0 or 3)
"""

import math
import time
import httpx
import pytest

from tests.deploy_utils import deploy_device, delete_device, _wait_for_devices_registered

pytestmark = [pytest.mark.integration, pytest.mark.integration_docker]

CONTROLLER_URL = "http://localhost:8080"
CLUSTER = "default"
EXPECTED_EDGES = {"edge-001", "edge-002", "edge-003"}
EXPECTED_SERVERS = {"server-001", "server-002"}
ALL_DEVICES = list(EXPECTED_SERVERS) + list(EXPECTED_EDGES)
ROUTING_WAIT_SEC = 60
ROUTING_POLL_INTERVAL_SEC = 2


@pytest.fixture(scope="module")
def routing(deployment_stack):
    """Deploy all devices, fetch the routing table, then clean up.

    Servers are deployed and confirmed registered with the controller *before*
    edges, so least-loaded balancing has both targets available when each edge
    calls POST /register.
    """
    for device_id in ALL_DEVICES:
        try:
            delete_device(device_id, controller_url=CONTROLLER_URL)
        except Exception:
            pass

    for server_id in EXPECTED_SERVERS:
        deploy_device(server_id, controller_url=CONTROLLER_URL)
    _wait_for_devices_registered(
        CONTROLLER_URL, list(EXPECTED_SERVERS), CLUSTER
    )

    for edge_id in EXPECTED_EDGES:
        deploy_device(edge_id, controller_url=CONTROLLER_URL)

    # Wait for inference containers to register and the routing table to populate.
    # deploy_device returns when the daemon has the container running, not when
    # the container itself has called POST /register on the controller.
    deadline = time.time() + ROUTING_WAIT_SEC
    result: list[dict] = []
    while time.time() < deadline:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{CONTROLLER_URL}/routing", params={"device_cluster": CLUSTER})
            resp.raise_for_status()
            rows = resp.json()["routing"]
        routed = [r for r in rows if r.get("target_device") is not None]
        if {r["source_device"] for r in routed} >= EXPECTED_EDGES:
            result = rows
            break
        time.sleep(ROUTING_POLL_INTERVAL_SEC)
    else:
        result = rows  # use last-fetched rows; tests will surface the mismatch

    for device_id in ALL_DEVICES:
        try:
            delete_device(device_id, controller_url=CONTROLLER_URL)
        except Exception:
            pass

    return result


def test_all_edges_are_routed(routing):
    """Every edge device must have exactly one routing entry."""
    routed_edges = {row["source_device"] for row in routing}
    assert routed_edges == EXPECTED_EDGES, (
        f"Expected routing entries for {EXPECTED_EDGES}, got {routed_edges}"
    )


def test_all_targets_are_valid_servers(routing):
    """Every routing entry must point to a known server."""
    for row in routing:
        assert row["target_device"] in EXPECTED_SERVERS, (
            f"Edge {row['source_device']} routed to unknown target: {row['target_device']}"
        )


def test_load_is_balanced(routing):
    """
    Least-connections should distribute edges as evenly as possible.
    With 3 edges and 2 servers: one server gets 2, the other gets 1.
    No server should be idle (0) or overloaded (>=3).
    """
    load: dict[str, int] = {}
    for row in routing:
        target = row["target_device"]
        load[target] = load.get(target, 0) + 1

    n_edges = len(EXPECTED_EDGES)
    n_servers = len(EXPECTED_SERVERS)
    max_allowed = math.ceil(n_edges / n_servers)  # 2

    for server, count in load.items():
        assert count <= max_allowed, (
            f"{server} has {count} edges assigned — exceeds max allowed ({max_allowed}). "
            f"Full load: {load}"
        )

    # Every server that exists in routing must have at least 1 edge
    for server in load:
        assert load[server] >= 1, f"{server} has 0 edges — should not appear in routing"


def test_all_entries_in_correct_cluster(routing):
    """All routing rows must belong to the default cluster."""
    for row in routing:
        assert row["source_cluster"] == CLUSTER, (
            f"Unexpected source_cluster: {row['source_cluster']}"
        )
        assert row["target_cluster"] == CLUSTER, (
            f"Unexpected target_cluster: {row['target_cluster']}"
        )

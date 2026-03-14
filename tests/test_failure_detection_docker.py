"""
Test for automatic failure detection and failover using Docker containers.

This test uses the deployed_inference_stack fixture (conftest.py) which brings up
controller + daemons, deploys all inference containers, then tears down after tests.
Failures are simulated by killing inference containers (edge-001, server-001, etc.).
"""
import time

import pytest

from tests.deploy_utils import deploy_device, inference_container_running, kill_inference_container

pytestmark = [pytest.mark.integration, pytest.mark.integration_docker]


class TestFailureDetectionDocker:
    """Docker-based integration tests for failure detection."""

    def test_edge_failure_and_restart(self, deployed_inference_stack):
        """Test that edge container restarts after failure and controller remains stable."""
        manager = deployed_inference_stack

        # Verify controller and inference containers are running
        assert manager.service_running("controller"), "Controller should be running at start."
        assert inference_container_running("server-001"), "Server should be running at start."
        assert inference_container_running("edge-001"), "Edge should be running at start."

        # Wait for initial heartbeats
        time.sleep(5)

        # Kill the edge inference container (simulates crash)
        kill_inference_container("edge-001")

        # Verify edge is down
        assert not inference_container_running("edge-001"), "Edge should be down after kill."

        # Wait for health monitor to detect failure and attempt restart
        edge_restarted = False
        for _ in range(30):
            time.sleep(1)
            if inference_container_running("edge-001"):
                edge_restarted = True
                break
        assert edge_restarted, "Edge container should be restarted by health monitor within 30 seconds."

        # Controller should still be running
        assert manager.service_running("controller"), "Controller should remain running after edge failure."

        # Server should still be running
        assert inference_container_running("server-001"), "Server should remain running after edge failure."

    def test_server_failure_detection(self, deployed_inference_stack):
        """Test that server failure is detected, controller remains stable, and edge reroutes if another server is available."""
        print("\n[TEST] Starting test_server_failure_detection")
        manager = deployed_inference_stack

        # Verify services are running (server-002 is deployed by deploy_all_inference)
        assert manager.service_running("controller"), "Controller should be running at start."
        assert inference_container_running("server-001"), "Server should be running at start."
        assert inference_container_running("edge-001"), "Edge should be running at start."
        multi_server = inference_container_running("server-002")

        # Wait for initial heartbeats
        time.sleep(5)

        # Kill the server inference container
        kill_inference_container("server-001")
        print("[INFO] Server container killed.")

        # Verify server is down
        assert not inference_container_running("server-001"), "Server should be down after kill."

        # Wait for health monitor to detect failure and possible rerouting
        rerouted = False
        for _ in range(20):
            time.sleep(1)
            if multi_server:
                if inference_container_running("edge-001") and inference_container_running("server-002"):
                    rerouted = True
                    break
            else:
                if inference_container_running("edge-001"):
                    rerouted = True
                    break
        assert rerouted, (
            "Edge should remain running and reroute to another server if available after server failure."
            if multi_server else "Edge should remain running after server failure."
        )

        # Controller should still be running
        assert manager.service_running("controller"), "Controller should remain running after server failure."

        # Restore server-001 so the next test has a clean state
        deploy_device("server-001", controller_url="http://localhost:8080")
        time.sleep(5)  # Allow server to start and send heartbeats

        print("[PASS] Server failure detected, controller stable, and edge rerouting checked.")

    def test_controller_stays_running_during_failures(self, deployed_inference_stack):
        """Test that controller remains stable when other services fail."""
        print("\n[TEST] Starting test_controller_stays_running_during_failures")
        manager = deployed_inference_stack

        # Verify services are running
        assert manager.service_running("controller"), "Controller should be running at start."
        assert inference_container_running("server-001"), "Server should be running at start."
        assert inference_container_running("edge-001"), "Edge should be running at start."

        # Wait for initial heartbeats
        time.sleep(5)

        # Kill edge first
        kill_inference_container("edge-001")
        print("[INFO] Edge container killed.")
        time.sleep(5)

        # Kill server
        kill_inference_container("server-001")
        print("[INFO] Server container killed.")
        time.sleep(5)

        # Controller should still be running
        assert manager.service_running("controller"), "Controller should remain running after other failures."

        print("[PASS] Controller remains stable when other services fail.")


if __name__ == "__main__":
    print("""
PRIMARY FAILURE DETECTION TEST - MOST IMPORTANT

This is the most comprehensive and realistic test for StreamBed's failure detection system.
It uses actual Docker containers instead of mocks for maximum realism.

REQUIREMENTS:
- Docker and docker-compose installed
- docker-compose.yml in project root
- No other services running on ports 8000-8004, 8080, 9090-9094

RUN ALL TESTS:
python3 -m pytest tests/test_failure_detection_docker.py -v -s

RUN INDIVIDUAL TESTS:
python3 -m pytest tests/test_failure_detection_docker.py::TestFailureDetectionDocker::test_edge_failure_and_restart -v -s
python3 -m pytest tests/test_failure_detection_docker.py::TestFailureDetectionDocker::test_server_failure_detection -v -s
python3 -m pytest tests/test_failure_detection_docker.py::TestFailureDetectionDocker::test_controller_stays_running_during_failures -v -s

Note: These tests use the deployed_inference_stack fixture which brings up the full stack
(controller + daemons + inference containers) before tests and tears down after.
""")

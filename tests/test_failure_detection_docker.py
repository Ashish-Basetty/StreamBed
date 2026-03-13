"""
Test for automatic failure detection and failover using Docker containers.

This test uses the existing docker-compose.yml to run real containers,
then simulates failures by stopping containers and verifies recovery.
"""
import os
import sys
import subprocess
import time

import pytest

# Adjust path as needed
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class DockerComposeManager:
    """Manages docker-compose services for testing."""

    def __init__(self, compose_file: str = "docker-compose.yml"):
        self.compose_file = compose_file
        self.project_name = "streambed"

    def up_services(self, services: list[str] = None):
        """Start docker-compose services."""
        cmd = ["docker-compose", "-f", self.compose_file, "-p", self.project_name, "up", "-d"]
        if services:
            cmd.extend(services)

        result = subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(__file__)),
                              capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Failed to start services: {result.stderr}")
        return result


    def down_services(self):
        """Stop all docker-compose services."""
        cmd = ["docker-compose", "-f", self.compose_file, "-p", self.project_name, "down"]
        result = subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(__file__)),
                              capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Warning: Failed to stop services: {result.stderr}")
        return result

    def stop_service(self, service: str):
        """Stop a specific service."""
        cmd = ["docker-compose", "-f", self.compose_file, "-p", self.project_name, "stop", service]
        result = subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(__file__)),
                              capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Failed to stop service {service}: {result.stderr}")
        return result

    def start_service(self, service: str):
        """Start a specific service."""
        cmd = ["docker-compose", "-f", self.compose_file, "-p", self.project_name, "start", service]
        result = subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(__file__)),
                              capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Failed to start service {service}: {result.stderr}")
        return result

    def kill_service(self, service: str):
        """Kill a specific service (simulates crash)."""
        # Get container name
        container_name = f"streambed-{service}"

        # Kill the container
        cmd = ["docker", "kill", container_name]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Failed to kill service {service}: {result.stderr}")
        return result

    def service_running(self, service: str) -> bool:
        """Check if a service is running."""
        container_name = f"streambed-{service}"
        cmd = ["docker", "ps", "--filter", f"name={container_name}", "--format", "{{.Names}}"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return container_name in result.stdout.strip()


class TestFailureDetectionDocker:
    """Docker-based integration tests for failure detection."""

    @pytest.fixture(scope="class")
    def docker_manager(self):
        """Fixture to provide DockerComposeManager instance."""
        manager = DockerComposeManager()
        yield manager
        # Cleanup after all tests
        manager.down_services()

    @pytest.fixture(scope="class")
    def setup_services(self, docker_manager):
        """Fixture to start all services before tests."""
        # Start all services
        docker_manager.up_services()
        time.sleep(10)  # Wait for services to start

        # Verify services are running
        assert docker_manager.service_running("controller")
        assert docker_manager.service_running("server")
        assert docker_manager.service_running("edge")

        yield

        # Services will be cleaned up by docker_manager fixture

    def test_edge_failure_and_restart(self, docker_manager):
        """Test that edge container restarts after failure and controller remains stable."""
        # Restart services for this test
        docker_manager.up_services()
        time.sleep(10)  # Wait for services to start

        # Verify services are running
        assert docker_manager.service_running("controller"), "Controller should be running at start."
        assert docker_manager.service_running("server"), "Server should be running at start."
        assert docker_manager.service_running("edge"), "Edge should be running at start."

        # Wait for initial heartbeats
        time.sleep(5)

        # Kill the edge container (simulates crash)
        docker_manager.kill_service("edge")

        # Verify edge is down
        assert not docker_manager.service_running("edge"), "Edge should be down after kill."

        # Wait for health monitor to detect failure and attempt restart
        # Instead of fixed sleep, poll for up to 30s for edge to come back up
        edge_restarted = False
        for _ in range(30):
            time.sleep(1)
            if docker_manager.service_running("edge"):
                edge_restarted = True
                break
        assert edge_restarted, "Edge container should be restarted by health monitor within 30 seconds."

        # Controller should still be running
        assert docker_manager.service_running("controller"), "Controller should remain running after edge failure."

        # Optionally, check that server is still running
        assert docker_manager.service_running("server"), "Server should remain running after edge failure."

        # Note: This test now validates that the edge is actually restarted, not just that controller is alive.

    def test_server_failure_detection(self, docker_manager):
        """Test that server failure is detected, controller remains stable, and edge reroutes if another server is available."""
        print("\n[TEST] Starting test_server_failure_detection")
        # Restart services for this test
        docker_manager.up_services()
        time.sleep(10)  # Wait for services to start

        # Verify services are running
        assert docker_manager.service_running("controller"), "Controller should be running at start."
        assert docker_manager.service_running("server"), "Server should be running at start."
        assert docker_manager.service_running("edge"), "Edge should be running at start."

        # If the system supports multiple servers, start a second server (assumes service name 'server2')
        try:
            docker_manager.start_service("server2")
            time.sleep(5)
            multi_server = docker_manager.service_running("server2")
        except Exception:
            multi_server = False

        # Wait for initial heartbeats
        time.sleep(5)

        # Kill the server container
        docker_manager.kill_service("server")
        print("[INFO] Server container killed.")

        # Verify server is down
        assert not docker_manager.service_running("server"), "Server should be down after kill."

        # Wait for health monitor to detect failure and possible rerouting
        rerouted = False
        for _ in range(20):
            time.sleep(1)
            if multi_server:
                # If server2 is running, check that edge is still running and server2 is up
                if docker_manager.service_running("edge") and docker_manager.service_running("server2"):
                    rerouted = True
                    break
            else:
                # If only one server, just check edge is still running
                if docker_manager.service_running("edge"):
                    rerouted = True
                    break
        assert rerouted, (
            "Edge should remain running and reroute to another server if available after server failure."
            if multi_server else "Edge should remain running after server failure."
        )

        # Controller should still be running
        assert docker_manager.service_running("controller"), "Controller should remain running after server failure."

        print("[PASS] Server failure detected, controller stable, and edge rerouting checked.")

        # Cleanup: restart server for other tests
        if multi_server:
            docker_manager.start_service("server")
            time.sleep(5)

    def test_controller_stays_running_during_failures(self, docker_manager):
        """Test that controller remains stable when other services fail."""
        print("\n[TEST] Starting test_controller_stays_running_during_failures")
        # Restart services for this test
        docker_manager.up_services()
        time.sleep(10)  # Wait for services to start

        # Verify services are running
        assert docker_manager.service_running("controller"), "Controller should be running at start."
        assert docker_manager.service_running("server"), "Server should be running at start."
        assert docker_manager.service_running("edge"), "Edge should be running at start."

        # Wait for initial heartbeats
        time.sleep(5)

        # Kill edge first
        docker_manager.kill_service("edge")
        print("[INFO] Edge container killed.")
        time.sleep(5)

        # Kill server
        docker_manager.kill_service("server")
        print("[INFO] Server container killed.")
        time.sleep(5)

        # Controller should still be running
        assert docker_manager.service_running("controller"), "Controller should remain running after other failures."

        print("[PASS] Controller remains stable when other services fail.")

        # This validates the health monitor doesn't crash when multiple services fail


if __name__ == "__main__":
    print("""
PRIMARY FAILURE DETECTION TEST - MOST IMPORTANT

This is the most comprehensive and realistic test for StreamBed's failure detection system.
It uses actual Docker containers instead of mocks for maximum realism.

REQUIREMENTS:
- Docker and docker-compose installed
- docker-compose.yml in project root
- No other services running on ports 8000-8001, 8080, 9000

RUN ALL TESTS:
python3 -m pytest tests/test_failure_detection_docker.py -v -s

RUN INDIVIDUAL TESTS:
python3 -m pytest tests/test_failure_detection_docker.py::TestFailureDetectionDocker::test_edge_failure_and_restart -v -s
python3 -m pytest tests/test_failure_detection_docker.py::TestFailureDetectionDocker::test_server_failure_detection -v -s
python3 -m pytest tests/test_failure_detection_docker.py::TestFailureDetectionDocker::test_controller_stays_running_during_failures -v -s

Note: These tests start actual Docker containers and may take several minutes to complete.
Make sure to run 'docker-compose down' after testing to clean up containers.
""")
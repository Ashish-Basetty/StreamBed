"""
Integration tests for deployment: deploy and delete via controller API.

Uses the same docker-compose as other tests (controller + daemons).
No auto-deploy: deployment_stack fixture brings up the stack only; tests deploy/delete manually.
"""
import httpx
import pytest

from tests.deploy_utils import delete_device, deploy_device

pytestmark = [pytest.mark.integration, pytest.mark.integration_docker]

CONTROLLER_URL = "http://localhost:8080"


class TestDeploymentDocker:
    """Docker-based integration tests for deploy and delete via controller API."""

    def test_deploy(self, deployment_stack):
        """Deploy server-001 and edge-001 via controller API."""
        server_result = deploy_device("server-001", controller_url=CONTROLLER_URL)
        edge_result = deploy_device("edge-001", controller_url=CONTROLLER_URL)

        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{CONTROLLER_URL}/deployments", params={"device_cluster": "default"})
            resp.raise_for_status()
            deployments = {
                row["device_id"]: row
                for row in resp.json()["deployments"]
            }

        for device_id, result in (
            ("server-001", server_result),
            ("edge-001", edge_result),
        ):
            deployment = deployments[device_id]
            assert deployment["container_name"] == result["container_name"]
            assert deployment["container_hash"] == result["container_hash"]
            assert deployment["sidecar_name"] == result["sidecar_name"]
            assert deployment["status"] == "running"

    def test_delete(self, deployment_stack):
        """Delete server-001 and edge-001 via controller API."""
        result = delete_device("server-001", controller_url=CONTROLLER_URL)
        assert result.get("ok") is True
        result = delete_device("edge-001", controller_url=CONTROLLER_URL)
        assert result.get("ok") is True

        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{CONTROLLER_URL}/deployments", params={"device_cluster": "default"})
            resp.raise_for_status()
            deployments = {
                row["device_id"]
                for row in resp.json()["deployments"]
            }
        assert "server-001" not in deployments
        assert "edge-001" not in deployments

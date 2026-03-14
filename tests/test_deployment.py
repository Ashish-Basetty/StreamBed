"""
Integration tests for deployment: deploy and delete via controller API.

Uses the same docker-compose as other tests (controller + daemons).
No auto-deploy: deployment_stack fixture brings up the stack only; tests deploy/delete manually.
"""
import pytest

from tests.deploy_utils import deploy_device, delete_device

pytestmark = [pytest.mark.integration, pytest.mark.integration_docker]

CONTROLLER_URL = "http://localhost:8080"


class TestDeploymentDocker:
    """Docker-based integration tests for deploy and delete via controller API."""

    def test_deploy(self, deployment_stack):
        """Deploy server-001 and edge-001 via controller API."""
        deploy_device("server-001", controller_url=CONTROLLER_URL)
        deploy_device("edge-001", controller_url=CONTROLLER_URL)

    def test_delete(self, deployment_stack):
        """Delete server-001 and edge-001 via controller API."""
        result = delete_device("server-001", controller_url=CONTROLLER_URL)
        assert result.get("ok") is True
        result = delete_device("edge-001", controller_url=CONTROLLER_URL)
        assert result.get("ok") is True

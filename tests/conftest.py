"""
Pytest fixtures for StreamBed integration tests.
"""
import time

import pytest

from tests.deploy_utils import delete_all_inference, deploy_all_inference
from tests.docker_utils import DockerComposeManager


@pytest.fixture(scope="session")
def deployed_inference_stack():
    """
    Session-scoped fixture: brings up controller + daemons, deploys all inference
    containers, yields for tests, then deletes inference and tears down compose.
    """
    manager = DockerComposeManager(
        compose_file="docker-compose.yml",
        project_name="streambed",
    )
    manager.up_services()
    time.sleep(10)  # Allow controller and daemons to start

    deploy_all_inference(controller_url="http://localhost:8080")

    yield manager

    delete_all_inference(controller_url="http://localhost:8080")
    manager.down_services()

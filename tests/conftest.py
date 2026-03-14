"""
Pytest fixtures for StreamBed integration tests.
"""
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from tests.deploy_utils import (
    _wait_for_controller,
    _wait_for_daemons,
    delete_all_inference,
    delete_device,
    deploy_all_inference,
)
from tests.docker_utils import DockerComposeManager

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEST_FAILURE_LOGS_DIR = _PROJECT_ROOT / "tests" / "logs"
# Docker mounts ./controller/data:/app/data, so DB is at controller/data/controller.db
# Local runs use controller/ControllerNode/data/controller.db
_CONTROLLER_DB_PATH = _PROJECT_ROOT / "controller" / "data" / "controller.db"


@pytest.fixture(scope="session")
def deployed_inference_stack():
    """
    Session-scoped fixture: brings up controller + daemons, deploys all inference
    containers, yields for tests, then deletes inference and tears down compose.
    """
    # Clear controller DB so we start with a clean slate (avoids stale deployment state)
    if _CONTROLLER_DB_PATH.exists():
        _CONTROLLER_DB_PATH.unlink()

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


@pytest.fixture(scope="module")
def deployment_stack():
    """
    Module-scoped fixture: brings up controller + daemons only (no auto-deploy).
    For tests that manually deploy/delete via the controller API.
    """
    if _CONTROLLER_DB_PATH.exists():
        _CONTROLLER_DB_PATH.unlink()

    manager = DockerComposeManager(
        compose_file="docker-compose.yml",
        project_name="streambed",
    )
    manager.up_services()
    time.sleep(10)
    _wait_for_controller("http://localhost:8080")
    _wait_for_daemons()

    yield manager

    # Clean up deployed containers before tearing down
    for device_id in ("server-001", "edge-001"):
        try:
            delete_device(device_id, controller_url="http://localhost:8080")
        except Exception:
            pass
    manager.down_services()


def _save_controller_logs_on_failure(item, report):
    """Save controller Docker logs when an integration_docker test fails."""
    if report.when != "call" or not report.failed:
        return
    if "integration_docker" not in (m.name for m in item.iter_markers()):
        return
    _TEST_FAILURE_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = item.name.replace("/", "_").replace("::", "_")
    log_path = _TEST_FAILURE_LOGS_DIR / f"controller_{safe_name}_{timestamp}.log"
    try:
        result = subprocess.run(
            ["docker", "logs", "streambed-controller"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        content = result.stdout or ""
        if result.stderr:
            content += f"\n--- stderr ---\n{result.stderr}"
        log_path.write_text(content)
    except subprocess.TimeoutExpired:
        log_path.write_text("(timed out capturing logs)\n")
    except Exception as e:
        log_path.write_text(f"(failed to capture: {e})\n")
    print(f"\n[Controller logs saved to {log_path}]")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    _save_controller_logs_on_failure(item, report)

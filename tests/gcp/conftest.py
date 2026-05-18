"""GCP test fixtures.

These tests assume the cluster is already up (see tests/gcp/README.md). The
fixtures here do *not* run docker-compose locally — that's the whole point.
"""
from __future__ import annotations

import httpx
import pytest

# GCP cluster device IDs come from infra/gcp/vms.tf (locals.workers keys).
# Cluster name is the variables.tf default unless overridden.
GCP_CLUSTER = "gcp-test"
GCP_WORKER_DEVICE_IDS = ["server-01", "server-02", "edge-01", "edge-02"]


@pytest.fixture(scope="session")
def gcp_controller_reachable(controller_url: str) -> str:
    """Skip the test if the controller isn't reachable.

    Common cause: forgot to start the IAP tunnel or stopped the VMs.
    """
    try:
        with httpx.Client(timeout=3) as client:
            resp = client.get(f"{controller_url}/health")
            resp.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        pytest.skip(
            f"GCP controller at {controller_url} not reachable ({e}). "
            f"Start the cluster (./infra/gcp/vms.sh start) and the IAP tunnel."
        )
    return controller_url

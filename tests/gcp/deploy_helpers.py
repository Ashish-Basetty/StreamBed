"""Helpers for deploying inference containers against the GCP cluster.

Scoped narrowly to what the GCP tests need (one server + one edge in the
gcp-test cluster). Not a general replacement for tests/deploy_utils.py.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest

EDGE_IMAGE = "ashishbasetty/streambed-edge:latest"
SERVER_IMAGE = "ashishbasetty/streambed-server:latest"

# Controller's reserved internal IP — workers reach mock-video here.
# Pinned by `google_compute_address.controller_internal` in main.tf.
CONTROLLER_INTERNAL_IP = "10.10.0.2"
MOCK_VIDEO_PORT = 9200

# Stagger between deploys so PyTorch containers don't all load at once on
# the same physical host pool (mostly cosmetic on GCP where each daemon
# is on its own VM, but kept consistent with local behavior).
DEPLOY_STAGGER_SEC = 8


def deploy(
    controller_url: str,
    *,
    cluster: str,
    device_id: str,
    device_type: str,
    image: str,
    host_port: int,
    container_port: int,
    video_server_host: str | None = None,
    video_server_port: int | None = None,
) -> dict:
    payload: dict = {
        "device_cluster": cluster,
        "device_id": device_id,
        "device_type": device_type,
        "image": image,
        "host_port": host_port,
        "container_port": container_port,
    }
    if video_server_host:
        payload["video_server_host"] = video_server_host
        payload["video_server_port"] = video_server_port or MOCK_VIDEO_PORT
    with httpx.Client(timeout=120) as client:
        resp = client.post(f"{controller_url}/deploy", json=payload)
    if resp.status_code != 200:
        pytest.fail(
            f"/deploy {device_id} HTTP {resp.status_code}: {resp.text[:400]}"
        )
    return resp.json()


def delete(controller_url: str, *, cluster: str, device_id: str) -> None:
    with httpx.Client(timeout=60) as client:
        # Best-effort: don't fail teardown if delete returns 404 (already gone).
        try:
            client.request(
                "DELETE",
                f"{controller_url}/delete",
                json={"device_cluster": cluster, "device_id": device_id},
            )
        except Exception:
            pass


def get_status(controller_url: str, cluster: str) -> list[dict]:
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{controller_url}/status", params={"device_cluster": cluster})
    resp.raise_for_status()
    return resp.json().get("status", [])


def wait_until(predicate, *, timeout_s: float, interval_s: float = 1.0, what: str = ""):
    """Poll `predicate()` until it returns truthy, or fail after timeout."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    pytest.fail(f"timed out after {timeout_s}s waiting for {what or 'condition'}; last={last!r}")


@contextmanager
def deployed_edge_server_pair(
    controller_url: str,
    cluster: str,
    *,
    server_id: str = "server-01",
    edge_id: str = "edge-01",
) -> Iterator[None]:
    """Deploy a matched server+edge pair, yield, then guarantee cleanup."""
    deploy(
        controller_url,
        cluster=cluster,
        device_id=server_id,
        device_type="server",
        image=SERVER_IMAGE,
        host_port=8001,
        container_port=8001,
    )
    time.sleep(DEPLOY_STAGGER_SEC)
    deploy(
        controller_url,
        cluster=cluster,
        device_id=edge_id,
        device_type="edge",
        image=EDGE_IMAGE,
        host_port=8000,
        container_port=8000,
        video_server_host=CONTROLLER_INTERNAL_IP,
        video_server_port=MOCK_VIDEO_PORT,
    )
    try:
        yield
    finally:
        delete(controller_url, cluster=cluster, device_id=edge_id)
        delete(controller_url, cluster=cluster, device_id=server_id)

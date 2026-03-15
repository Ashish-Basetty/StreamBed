"""Shared utilities for StreamBed."""

import hashlib
import secrets

import docker


_docker_client: docker.DockerClient | None = None


def _get_docker() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


def _get_network(client: docker.DockerClient) -> str | None:
    with open("/etc/hostname") as f:
        container_id = f.read().strip()
    container = client.containers.get(container_id)
    networks = container.attrs["NetworkSettings"]["Networks"]
    network_name = list(networks.keys())[0] if networks else None
    return network_name


def _deployment_hash() -> str:
    """Generate a unique hash for this deployment."""
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()[:12]

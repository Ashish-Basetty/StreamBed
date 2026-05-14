"""Daemon-managed QUIC sidecar lifecycle.

Mirrors the inference-container pattern: the daemon owns docker-py and
spawns/kills its sidecar in lockstep with daemon lifetime. One sidecar per
daemon, named `streambed-{cluster}-{device_id}-sidecar`.
"""

import logging

import docker
from docker.types import LogConfig

from shared.docker_labels import ROLE_SIDECAR, managed_labels
from shared.utils import _get_docker, _get_network

logger = logging.getLogger(__name__)


def _container_name(cluster: str, device_id: str) -> str:
    return f"streambed-{cluster}-{device_id}-sidecar"


def spawn_sidecar(
    *,
    cluster: str,
    device_id: str,
    role: str,
    image: str,
    daemon_url: str,
    peer_quic_port: int,
    local_udp_bind: str,
    quic_bind: str
) -> str | None:
    """Launch the sidecar container. Idempotent: removes any existing one first.

    Returns the new container name or None on failure.

    Edge role: polls daemon_url/stream-target every 15 s for the current peer.
    """
    name = _container_name(cluster, device_id)
    try:
        client = _get_docker()
    except Exception as e:
        logger.warning(f"[Daemon] sidecar: docker unavailable, skipping spawn: {e}")
        return None

    # Wipe any straggler from a prior crash before re-creating.
    try:
        existing = client.containers.get(name)
        existing.remove(force=True)
    except docker.errors.NotFound:
        pass
    except Exception as e:
        logger.warning(f"[Daemon] sidecar: failed to remove stale container {name}: {e}")

    env = {
        "SIDECAR_ROLE": role,
        "LOCAL_UDP_BIND": local_udp_bind,
        "QUIC_BIND": quic_bind,
        "DEVICE_ID": device_id,
        "DEVICE_CLUSTER": cluster,
        "DAEMON_URL": daemon_url,
        "PEER_QUIC_PORT": str(peer_quic_port),
    }

    run_kwargs: dict = {
        "name": name,
        "detach": True,
        "environment": env,
        # Explicit json-file so `docker logs` always reads from the default file driver
        # (avoids empty-log edge cases on some Docker Desktop setups).
        "log_config": LogConfig(type="json-file"),
        # METADATA ONLY. The controller deployments table owns deployment
        # state; labels are just a recovery handle for orphaned Docker resources.
        "labels": managed_labels(cluster=cluster, device_id=device_id, role=ROLE_SIDECAR),
    }
    try:
        network = _get_network(client)
        if network:
            run_kwargs["network"] = network
    except Exception:
        # Outside docker (local dev), `_get_network` reads /etc/hostname; tolerate.
        pass

    try:
        client.containers.run(image, **run_kwargs)
        logger.info(f"[Daemon] sidecar spawned: {name} role={role} daemon={daemon_url} network={network}")
        return name
    except docker.errors.ImageNotFound:
        logger.error(f"[Daemon] sidecar: image not found: {image}")
        return None
    except Exception as e:
        logger.error(f"[Daemon] sidecar: spawn failed for {name}: {e}")
        return None


def kill_sidecar(*, cluster: str, device_id: str) -> None:
    """Best-effort kill+remove of this daemon's sidecar."""
    name = _container_name(cluster, device_id)
    try:
        client = _get_docker()
        container = client.containers.get(name)
        try:
            container.kill()
        except Exception:
            pass
        try:
            container.remove(force=True)
        except Exception:
            pass
        logger.info(f"[Daemon] sidecar killed: {name}")
    except docker.errors.NotFound:
        return
    except Exception as e:
        logger.warning(f"[Daemon] sidecar: kill failed for {name}: {e}")

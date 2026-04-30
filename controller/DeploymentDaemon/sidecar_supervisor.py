"""Daemon-managed QUIC sidecar lifecycle.

Mirrors the inference-container pattern: the daemon owns docker-py and
spawns/kills its sidecar in lockstep with daemon lifetime. One sidecar per
daemon, named `streambed-{cluster}-{device_id}-sidecar`.
"""

import logging

import docker

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
    peer_address: str | None,
    local_udp_bind: str,
    daemon_address: str,
    quic_bind: str,
    local_server_udp: str,
) -> str | None:
    """Launch the sidecar container. Idempotent: removes any existing one first.

    Returns the new container name or None on failure.
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
        "DAEMON_ADDRESS": daemon_address,
        "QUIC_BIND": quic_bind,
        "LOCAL_SERVER_UDP": local_server_udp,
        "DEVICE_ID": device_id,
        "DEVICE_CLUSTER": cluster,
    }
    if peer_address:
        env["PEER_ADDRESS"] = peer_address

    run_kwargs: dict = {
        "name": name,
        "detach": True,
        "environment": env,
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
        logger.info(f"[Daemon] sidecar spawned: {name} role={role} peer={peer_address}")
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

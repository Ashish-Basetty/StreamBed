"""StreamBed deployment daemon - pulls and runs containers from DockerHub."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

import docker
import httpx
import uvicorn
from daemon_config import (
    CONTROLLER_URL,
    DAEMON_ADDRESS,
    DAEMON_PORT,
    DEFAULT_CONTAINER_PORT,
    DEFAULT_HOST_PORT,
    DEVICE_CLUSTER,
    DEVICE_ID,
    DEVICE_TYPE,
    INGEST_UDP_PORT,
    REGISTER_RETRIES,
    REGISTER_RETRY_DELAY,
    SIDECAR_IMAGE,
    SIDECAR_LOCAL_UDP_PORT,
    SIDECAR_QUIC_BIND_PORT,
    SIDECAR_RECV_PORT,
    SIDECAR_SERVER_REVERSE_BIND_PORT,
    STATE_PATH,
    STREAM_TARGET_PATH,
    STREAMBED_CONFIG_HOST_PATH,
    STREAMBED_DATA_HOST_PATH,
    STREAMBED_MEMORY_LIMIT,
    VIDEO_SERVER_HOST,
    VIDEO_SERVER_PORT,
)
from fastapi import FastAPI
from pydantic import BaseModel
from sidecar_supervisor import kill_sidecar, spawn_sidecar

from shared.docker_labels import ROLE_INFERENCE, managed_label_filters, managed_labels
from shared.utils import _deployment_hash, _get_docker, _get_network

# Configure logging (same format as controller)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class DeployRequest(BaseModel):
    image: str
    host_port: int | None = None  # defaults to STREAMBED_HOST_PORT
    container_port: int | None = None  # defaults to STREAMBED_CONTAINER_PORT
    controller_url: str | None = None  # defaults to CONTROLLER_URL
    video_server_host: str | None = None  # overrides daemon's VIDEO_SERVER_HOST
    video_server_port: int | None = None  # overrides daemon's VIDEO_SERVER_PORT


class StreamTargetRequest(BaseModel):
    target_ip: str
    target_port: int


class DeleteRequest(BaseModel):
    container_name: str | None = None
    sidecar_name: str | None = None


def _load_state() -> dict | None:
    """Load last deployed container state from JSON."""
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_state(container_hash: str, image: str) -> None:
    """Persist deployed container state."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"container_hash": container_hash, "image": image}, indent=2))


def _stop_and_remove(container_name: str) -> bool:
    """Stop and remove a container. Returns False if it does not exist."""
    try:
        client = _get_docker()
        container = client.containers.get(container_name)
        if container.status == "running":
            container.stop(timeout=30)
        container.remove(force=True)
        return True
    except docker.errors.NotFound:
        return False


def _load_stream_target() -> dict | None:
    """Load stream target config from shared volume."""
    if not STREAM_TARGET_PATH.exists():
        return None
    try:
        return json.loads(STREAM_TARGET_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_stream_target(target_ip: str, target_port: int) -> None:
    """Write stream target config to shared volume. Containers read this via -v mount."""
    STREAM_TARGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    STREAM_TARGET_PATH.write_text(
        json.dumps({"target_ip": target_ip, "target_port": target_port}, indent=2)
    )


async def _register_with_retries() -> None:
    url = f"{CONTROLLER_URL.rstrip('/')}/register"
    payload = {
        "device_cluster": DEVICE_CLUSTER,
        "device_id": DEVICE_ID,
        "device_type": DEVICE_TYPE,
        "ip": DAEMON_ADDRESS,
        "port": DAEMON_PORT
    }
    last_err: Exception | None = None
    for attempt in range(REGISTER_RETRIES):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                return
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_err = e
            if attempt < REGISTER_RETRIES - 1:
                await asyncio.sleep(REGISTER_RETRY_DELAY)
    raise last_err


async def _deregister_with_retries() -> None:
    url = f"{CONTROLLER_URL.rstrip('/')}/deregister"
    payload = {
        "device_cluster": DEVICE_CLUSTER,
        "device_id": DEVICE_ID
    }
    last_err: Exception | None = None
    for attempt in range(REGISTER_RETRIES):
        try:
            async with httpx.AsyncClient() as client:
                await client.delete(url, json=payload)
                return
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_err = e
            if attempt < REGISTER_RETRIES - 1:
                await asyncio.sleep(REGISTER_RETRY_DELAY)
    raise last_err


def _wait_running(client, name: str, timeout: int = 30) -> None:
    """Block until the named container status is 'running', or timeout expires."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            c = client.containers.get(name)
            if c.status == "running":
                return
        except docker.errors.NotFound:
            pass
        time.sleep(0.5)
    logger.warning(f"[Daemon] container {name} not running after {timeout}s")


def _spawn_sidecar_for_role() -> str | None:
    """Spawn the QUIC sidecar matching this daemon's role.

    Reverse-path wiring is opt-in:
      - Edge sidecar gets LOCAL_RECV_UDP_TARGET=<DEVICE_ID>:SIDECAR_RECV_PORT
        when SIDECAR_RECV_PORT > 0. The local inference container must be on
        the same docker network and have an alias = DEVICE_ID for that name
        to resolve. /deploy attaches the alias for both server and edge.
      - Server sidecar gets SERVER_REVERSE_UDP_BIND=0.0.0.0:SIDECAR_SERVER_REVERSE_BIND_PORT
        when SIDECAR_SERVER_REVERSE_BIND_PORT > 0.
    """
    role = "edge" if DEVICE_TYPE == "edge" else "server"
    local_recv_udp_target = ""
    server_reverse_udp_bind = ""
    if role == "edge" and SIDECAR_RECV_PORT > 0:
        local_recv_udp_target = f"{DEVICE_ID}:{SIDECAR_RECV_PORT}"
    if role == "server" and SIDECAR_SERVER_REVERSE_BIND_PORT > 0:
        server_reverse_udp_bind = f"0.0.0.0:{SIDECAR_SERVER_REVERSE_BIND_PORT}"
    # Server sidecar delivers frames to the local inference container by network alias.
    local_server_udp = f"{DEVICE_ID}:{INGEST_UDP_PORT}" if role == "server" else ""
    return spawn_sidecar(
        cluster=DEVICE_CLUSTER,
        device_id=DEVICE_ID,
        role=role,
        image=SIDECAR_IMAGE,
        daemon_url=f"http://{DAEMON_ADDRESS}:{DAEMON_PORT}",
        peer_quic_port=SIDECAR_QUIC_BIND_PORT,
        local_udp_bind=f"0.0.0.0:{SIDECAR_LOCAL_UDP_PORT}",
        quic_bind=f"0.0.0.0:{SIDECAR_QUIC_BIND_PORT}",
        local_server_udp=local_server_udp,
        local_recv_udp_target=local_recv_udp_target,
        server_reverse_udp_bind=server_reverse_udp_bind,
    )


def _remove_managed_inference_containers(client: docker.DockerClient) -> int:
    """Remove daemon-owned inference containers for this device.

    CONTROLLER DB IS THE SOURCE OF TRUTH FOR DEPLOYMENT STATE.
    LABELS BELOW ARE METADATA ONLY, USED ONLY AS A RECOVERY SWEEP FOR ORPHANED
    DOCKER RESOURCES WHEN THE CONTROLLER/DAEMON STATE IS MISSING OR STALE.
    """
    prefix = f"streambed-{DEVICE_CLUSTER}-{DEVICE_ID}-"
    sidecar_name = f"{prefix}sidecar"
    containers_by_id = {}
    removed = 0
    for container in client.containers.list(
        all=True,
        filters={
            "label": managed_label_filters(
                cluster=DEVICE_CLUSTER,
                device_id=DEVICE_ID,
                role=ROLE_INFERENCE,
            )
        },
    ):
        containers_by_id[container.id] = container
    # Legacy fallback: pre-label containers used this deterministic prefix.
    for container in client.containers.list(all=True):
        if container.name.startswith(prefix) and container.name != sidecar_name:
            containers_by_id[container.id] = container

    for container in containers_by_id.values():
        if _stop_and_remove(container.name):
            removed += 1
    return removed


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _register_with_retries()

    yield

    try:
        _remove_managed_inference_containers(_get_docker())
    except Exception:
        logger.exception("[Daemon] failed to clean inference containers during shutdown")
    kill_sidecar(cluster=DEVICE_CLUSTER, device_id=DEVICE_ID)
    await _deregister_with_retries()


app = FastAPI(title="StreamBed Deployment Daemon", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/deploy")
def deploy(body: DeployRequest) -> dict:
    """
    Pull image and run new container with port mapping.
    Must stop old container first to free the host port (no way to change ports on running containers).
    On pull failure, leaves existing container untouched. On run failure after stop, returns error.
    """
    if not body.image.strip():
        return {"ok": False, "error": "image is required"}

    host_port = body.host_port if body.host_port is not None else DEFAULT_HOST_PORT
    container_port = body.container_port if body.container_port is not None else DEFAULT_CONTAINER_PORT
    deploy_hash = _deployment_hash()
    new_container = f"streambed-{DEVICE_CLUSTER}-{DEVICE_ID}-{deploy_hash}"

    try:
        client = _get_docker()

        network = _get_network(client)

        # 1. Pull the new image (if this fails, old container stays running)
        client.images.pull(body.image)

        # 2. Stop old/stale containers to free the host port.
        _remove_managed_inference_containers(client)

        # 3. Run new container with port mapping, volumes, and memory limit (PyTorch needs ~1–2GB)
        run_kwargs = {
            "name": new_container,
            "detach": True,
            "ports": {f"{container_port}/tcp": host_port},
            "mem_limit": STREAMBED_MEMORY_LIMIT,
            # METADATA ONLY. The controller deployments table is authoritative;
            # these labels are only for orphan cleanup/recovery.
            "labels": managed_labels(
                cluster=DEVICE_CLUSTER,
                device_id=DEVICE_ID,
                role=ROLE_INFERENCE,
            ),
        }
        volumes = {}
        if STREAMBED_CONFIG_HOST_PATH:
            volumes[STREAMBED_CONFIG_HOST_PATH] = {"bind": "/config", "mode": "ro"}
        if STREAMBED_DATA_HOST_PATH:
            volumes[STREAMBED_DATA_HOST_PATH] = {"bind": "/data/streambed", "mode": "rw"}
        if volumes:
            run_kwargs["volumes"] = volumes

        container_env = {
            "DEVICE_ID": DEVICE_ID,
            "DEVICE_CLUSTER": DEVICE_CLUSTER,
            "CONTROLLER_URL": CONTROLLER_URL,
            "VIDEO_SERVER_HOST": body.video_server_host or VIDEO_SERVER_HOST,
            "VIDEO_SERVER_PORT": str(body.video_server_port or VIDEO_SERVER_PORT),
        }
        sidecar_name = f"streambed-{DEVICE_CLUSTER}-{DEVICE_ID}-sidecar"
        container_env["SIDECAR_HOST"] = sidecar_name
        if DEVICE_TYPE == "edge":
            container_env["SIDECAR_FEED_PORT"] = str(SIDECAR_LOCAL_UDP_PORT)
            if SIDECAR_RECV_PORT > 0:
                container_env["ADVICE_LISTEN_PORT"] = str(SIDECAR_RECV_PORT)
        else:
            container_env["FEED_LISTEN_PORT"] = str(INGEST_UDP_PORT)
            if SIDECAR_SERVER_REVERSE_BIND_PORT > 0:
                container_env["SIDECAR_REVERSE_PORT"] = str(SIDECAR_SERVER_REVERSE_BIND_PORT)
        run_kwargs["environment"] = container_env

        container = client.containers.create(body.image, **run_kwargs)
        if network:
            client.networks.get(network).connect(container, aliases=[DEVICE_ID])
        container.start()
        _save_state(deploy_hash, body.image)

        sidecar_name = _spawn_sidecar_for_role()

        # Wait for both containers to reach "running" before returning so that
        # callers (e.g. deploy scripts) can safely proceed.  The inference
        # container is registered in Docker DNS as soon as containers.run()
        # returns, so the sidecar's lazy DNS resolution (per-peer, not at
        # startup) will succeed whenever the first QUIC peer connects.
        _wait_running(client, new_container, timeout=60)
        if sidecar_name:
            _wait_running(client, sidecar_name, timeout=30)

        return {
            "ok": True,
            "device_cluster": DEVICE_CLUSTER,
            "device_id": DEVICE_ID,
            "container_hash": deploy_hash,
            "container_name": new_container,
            "sidecar_name": sidecar_name,
        }
    except docker.errors.ImageNotFound:
        _stop_and_remove(new_container)
        logger.error("[Daemon] /deploy failed: image not found: %s", body.image)
        return {"ok": False, "error": f"Image not found: {body.image}"}
    except docker.errors.APIError as e:
        _stop_and_remove(new_container)
        logger.error("[Daemon] /deploy Docker API error: %s", e)
        return {"ok": False, "error": str(e)}
    except Exception as e:
        _stop_and_remove(new_container)
        logger.exception("[Daemon] /deploy unexpected error")
        return {"ok": False, "error": str(e)}

@app.delete("/delete")
def delete(body: DeleteRequest | None = None) -> dict:
    """Delete the streambed inference container(s) managed by this daemon, and the sidecar."""
    try:
        client = _get_docker()
        removed = 0
        if body and body.container_name:
            if _stop_and_remove(body.container_name):
                removed += 1
        else:
            removed += _remove_managed_inference_containers(client)
        if body and body.sidecar_name:
            _stop_and_remove(body.sidecar_name)
        else:
            kill_sidecar(cluster=DEVICE_CLUSTER, device_id=DEVICE_ID)
        return {"ok": True, "removed": removed}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/stream-target")
def get_stream_target() -> dict:
    """Return current stream target config. Containers read this from the shared volume."""
    config = _load_stream_target()
    if config is None:
        return {"target_ip": None, "target_port": None}
    return config


@app.put("/stream-target")
def put_stream_target(body: StreamTargetRequest) -> dict:
    """Update stream target config in shared volume. Containers can poll this file for changes."""
    _save_stream_target(body.target_ip, body.target_port)
    return {"ok": True, "target_ip": body.target_ip, "target_port": body.target_port}


if __name__ == "__main__":
    logger.info(f"Deployment daemon running on port {DAEMON_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=DAEMON_PORT, log_level="info")

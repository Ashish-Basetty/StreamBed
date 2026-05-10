"""StreamBed deployment daemon - pulls and runs containers from DockerHub."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

import docker
import httpx
import uvicorn
from daemon_config import (
    BANDWIDTH_POLL_INTERVAL,
    CONTROLLER_URL,
    DAEMON_ADDRESS,
    DAEMON_PORT,
    DEFAULT_CONTAINER_PORT,
    DEFAULT_HOST_PORT,
    DEVICE_CLUSTER,
    DEVICE_ID,
    DEVICE_TYPE,
    MAX_FRAME_PAYLOAD_BYTES,
    REGISTER_RETRIES,
    REGISTER_RETRY_DELAY,
    SIDECAR_IMAGE,
    SIDECAR_LOCAL_UDP_PORT,
    SIDECAR_PEER_ADDRESS,
    SIDECAR_QUIC_BIND_PORT,
    SIDECAR_RECV_PORT,
    SIDECAR_SERVER_REVERSE_BIND_PORT,
    STATE_PATH,
    STREAM_PROXY_HOST,
    STREAM_PROXY_PORT,
    STREAM_TARGET_PATH,
    STREAM_TARGET_POLL_INTERVAL,
    STREAM_TRANSPORT,
    STREAMBED_CONFIG_HOST_PATH,
    STREAMBED_DATA_HOST_PATH,
    STREAMBED_MEMORY_LIMIT,
    VIDEO_SOURCE,
)
from fastapi import FastAPI
from pydantic import BaseModel
from sidecar_supervisor import kill_sidecar, spawn_sidecar
from stream_proxy_manager import StreamProxyManager
from tcp_utils import _UDPSendOnlyProtocol, handle_tcp_stream

from shared.bandwidth import SentRateBackend
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


class StreamTargetRequest(BaseModel):
    target_ip: str
    target_port: int


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


def _stop_and_remove(container_name: str) -> None:
    """Stop and remove a container. Ignores errors (container may not exist)."""
    try:
        client = _get_docker()
        container = client.containers.get(container_name)
        container.stop(timeout=30)
        container.remove()
    except docker.errors.NotFound:
        pass


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


async def _stream_proxy_target_poll_loop(manager: StreamProxyManager) -> None:
    """Periodically reload stream-target.json and update proxy destination."""
    while True:
        cfg = _load_stream_target()
        if cfg and cfg.get("target_ip") and cfg.get("target_port") is not None:
            manager.set_target(cfg["target_ip"], int(cfg["target_port"]))
        await asyncio.sleep(STREAM_TARGET_POLL_INTERVAL)


async def _bandwidth_poll_loop(manager: StreamProxyManager) -> None:
    """Periodically poll get_target_bps from the estimator and update target_bitrate."""
    while True:
        estimator = manager.get_estimator()
        if estimator is not None:
            manager.update_target_bitrate(estimator.get_target_bps())
        await asyncio.sleep(BANDWIDTH_POLL_INTERVAL)


async def _run_stream_tcp_server(manager: StreamProxyManager) -> None:
    """Start TCP server for edge connections and UDP transport for forwarding. Edge daemons only."""
    loop = asyncio.get_running_loop()
    udp_transport, _ = await loop.create_datagram_endpoint(
        lambda: _UDPSendOnlyProtocol(),
        local_addr=("0.0.0.0", 0),
    )
    manager.set_udp_transport(udp_transport)
    server = await asyncio.start_server(
        lambda r, w: handle_tcp_stream(r, w, manager, MAX_FRAME_PAYLOAD_BYTES),
        "0.0.0.0",
        STREAM_PROXY_PORT,
    )
    logger.info(f"[Daemon] Stream TCP server on 0.0.0.0:{STREAM_PROXY_PORT} (target from stream-target.json)")
    await server.serve_forever()


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


async def _cancel_task(task: asyncio.Task | None) -> None:
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


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
    """Spawn the QUIC sidecar matching this daemon's role. No-op unless STREAM_TRANSPORT=quic.

    Reverse-path wiring is opt-in:
      - Edge sidecar gets LOCAL_RECV_UDP_TARGET=<DEVICE_ID>:SIDECAR_RECV_PORT
        when SIDECAR_RECV_PORT > 0. The local inference container must be on
        the same docker network and have an alias = DEVICE_ID for that name
        to resolve. /deploy attaches the alias for both server and edge.
      - Server sidecar gets SERVER_REVERSE_UDP_BIND=0.0.0.0:SIDECAR_SERVER_REVERSE_BIND_PORT
        when SIDECAR_SERVER_REVERSE_BIND_PORT > 0.
    """
    if STREAM_TRANSPORT != "quic":
        return None
    role = "edge" if DEVICE_TYPE == "edge" else "server"
    local_recv_udp_target = ""
    server_reverse_udp_bind = ""
    if role == "edge" and SIDECAR_RECV_PORT > 0:
        local_recv_udp_target = f"{DEVICE_ID}:{SIDECAR_RECV_PORT}"
    if role == "server" and SIDECAR_SERVER_REVERSE_BIND_PORT > 0:
        server_reverse_udp_bind = f"0.0.0.0:{SIDECAR_SERVER_REVERSE_BIND_PORT}"
    # The server sidecar must reach its local inference container by network
    # alias, not 127.0.0.1 (which would loop within the sidecar's namespace).
    local_server_udp = (
        f"{DEVICE_ID}:{STREAM_PROXY_PORT}" if role == "server"
        else f"127.0.0.1:{STREAM_PROXY_PORT}"
    )
    return spawn_sidecar(
        cluster=DEVICE_CLUSTER,
        device_id=DEVICE_ID,
        role=role,
        image=SIDECAR_IMAGE,
        peer_address=SIDECAR_PEER_ADDRESS or None,
        local_udp_bind=f"0.0.0.0:{SIDECAR_LOCAL_UDP_PORT}",
        quic_bind=f"0.0.0.0:{SIDECAR_QUIC_BIND_PORT}",
        local_server_udp=local_server_udp,
        local_recv_udp_target=local_recv_udp_target,
        server_reverse_udp_bind=server_reverse_udp_bind,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _register_with_retries()

    poll_task = None
    proxy_task = None
    bandwidth_task = None
    stream_proxy_manager = StreamProxyManager()
    if DEVICE_TYPE == "edge":
        # Edge owns the rate decision. Sole input is locally-observed throughput;
        # QUIC's congestion control closes the loop at the transport layer, so
        # received_bps app-feedback was redundant and is gone.
        stream_proxy_manager.set_estimator(SentRateBackend())
        poll_task = asyncio.create_task(_stream_proxy_target_poll_loop(stream_proxy_manager))
        proxy_task = asyncio.create_task(_run_stream_tcp_server(stream_proxy_manager))
        bandwidth_task = asyncio.create_task(_bandwidth_poll_loop(stream_proxy_manager))

    yield

    await _cancel_task(bandwidth_task)
    await _cancel_task(poll_task)
    await _cancel_task(proxy_task)
    if proxy_task:
        stream_proxy_manager.close()
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

        # 2. Stop old container to free the host port (can't have two containers on same port)
        state = _load_state()
        if state:
            old_container = f"streambed-{DEVICE_CLUSTER}-{DEVICE_ID}-{state['container_hash']}"
            _stop_and_remove(old_container)

        # 3. Run new container with port mapping, volumes, and memory limit (PyTorch needs ~1–2GB)
        run_kwargs = {
            "name": new_container,
            "detach": True,
            "ports": {f"{container_port}/tcp": host_port},
            "mem_limit": STREAMBED_MEMORY_LIMIT,
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
        }
        if VIDEO_SOURCE:
            container_env["VIDEO_SOURCE"] = VIDEO_SOURCE
        if DEVICE_TYPE == "edge":
            container_env["STREAM_PROXY_HOST"] = STREAM_PROXY_HOST
            container_env["STREAM_PROXY_PORT"] = str(STREAM_PROXY_PORT)
        # QUIC sidecar coordinates for inference containers that talk UDP-direct
        # to the sidecar (advisor flow). Backward-compatible: the existing
        # edge/server containers ignore these. Edge gets SIDECAR_FEED_PORT
        # (where to send features) + ADVICE_LISTEN_PORT (where the sidecar
        # forwards advice in). Server gets SIDECAR_REVERSE_PORT (where to send
        # advice) + FEED_LISTEN_PORT (where the sidecar forwards features in).
        if STREAM_TRANSPORT == "quic":
            sidecar_name = f"streambed-{DEVICE_CLUSTER}-{DEVICE_ID}-sidecar"
            container_env["SIDECAR_HOST"] = sidecar_name
            if DEVICE_TYPE == "edge":
                container_env["SIDECAR_FEED_PORT"] = str(SIDECAR_LOCAL_UDP_PORT)
                if SIDECAR_RECV_PORT > 0:
                    container_env["ADVICE_LISTEN_PORT"] = str(SIDECAR_RECV_PORT)
            else:
                container_env["FEED_LISTEN_PORT"] = str(STREAM_PROXY_PORT)
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

        return {"ok": True, "container_hash": deploy_hash}
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
def delete() -> dict:
    """Delete the streambed inference container(s) managed by this daemon, and the sidecar."""
    try:
        client = _get_docker()
        containers = client.containers.list(filters={"status": "running"})
        prefix = f"streambed-{DEVICE_CLUSTER}-{DEVICE_ID}-"
        sidecar_name = f"{prefix}sidecar"
        # Inference containers only — exclude the sidecar; it's killed explicitly below.
        containers = [
            c for c in containers
            if c.name.startswith(prefix) and c.name != sidecar_name
        ]
        if len(containers) == 0:
            return {"ok": False, "error": "No containers running"}
        for container in containers:
            _stop_and_remove(container.name)
        kill_sidecar(cluster=DEVICE_CLUSTER, device_id=DEVICE_ID)
        return {"ok": True}
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

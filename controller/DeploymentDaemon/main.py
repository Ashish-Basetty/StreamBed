"""StreamBed deployment daemon - pulls and runs containers from DockerHub."""
import asyncio
import json
import logging
from typing import Callable

import docker
import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from contextlib import asynccontextmanager

from shared.bandwidth import CompositeBackend, SentRateBackend, ServerFeedbackBackend
from shared.utils import _deployment_hash, _get_docker, _get_network

from daemon_config import (
    BANDWIDTH_POLL_INTERVAL,
    CONTROLLER_URL,
    DAEMON_ADDRESS,
    DAEMON_PORT,
    DEFAULT_CONTAINER_PORT,
    DEFAULT_HOST_PORT,
    DEVICE_CLUSTER,
    DEVICE_ID,
    MAX_FRAME_PAYLOAD_BYTES,
    STATE_PATH,
    STREAM_PROXY_HOST,
    STREAM_PROXY_PORT,
    STREAM_TARGET_PATH,
    STREAM_TARGET_POLL_INTERVAL,
    STREAMBED_CONFIG_HOST_PATH,
    STREAMBED_DATA_HOST_PATH,
    STREAMBED_MEMORY_LIMIT,
    VIDEO_SOURCE,
    REGISTER_RETRIES,
    REGISTER_RETRY_DELAY,
)
from stream_proxy_manager import StreamProxyManager
from tcp_utils import _UDPSendOnlyProtocol, handle_tcp_stream


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


async def _run_stream_tcp_server(
    manager: StreamProxyManager,
    on_feedback_received: Callable[[dict], None] | None = None,
) -> None:
    """Start TCP server for edge connections and UDP transport for forwarding. Edge daemons only."""
    loop = asyncio.get_running_loop()
    udp_transport, _ = await loop.create_datagram_endpoint(
        lambda: _UDPSendOnlyProtocol(on_feedback_received=on_feedback_received),
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _register_with_retries()

    poll_task = None
    proxy_task = None
    bandwidth_task = None
    server_feedback = None
    stream_proxy_manager = StreamProxyManager()
    if DEVICE_ID.startswith("edge-"):
        sent_rate = SentRateBackend()
        server_feedback = ServerFeedbackBackend(default_bps=500_000)
        stream_proxy_manager.set_estimator(CompositeBackend(sent_rate, server_feedback))
        poll_task = asyncio.create_task(_stream_proxy_target_poll_loop(stream_proxy_manager))
        feedback_cb = server_feedback.update_from_response
        proxy_task = asyncio.create_task(
            _run_stream_tcp_server(stream_proxy_manager, on_feedback_received=feedback_cb)
        )
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

        if network:
            run_kwargs["network"] = network
            # Server containers get network alias = device_id so proxy can reach them at server-001:9000
            if DEVICE_ID.startswith("server-"):
                run_kwargs["networking_config"] = client.api.create_networking_config({
                    network: client.api.create_endpoint_config(aliases=[DEVICE_ID])
                })

        container_env = {
            "DEVICE_ID": DEVICE_ID,
            "DEVICE_CLUSTER": DEVICE_CLUSTER,
            "CONTROLLER_URL": CONTROLLER_URL,
        }
        if VIDEO_SOURCE:
            container_env["VIDEO_SOURCE"] = VIDEO_SOURCE
        if DEVICE_ID.startswith("edge-"):
            container_env["STREAM_PROXY_HOST"] = STREAM_PROXY_HOST
            container_env["STREAM_PROXY_PORT"] = str(STREAM_PROXY_PORT)
        run_kwargs["environment"] = container_env

        client.containers.run(body.image, **run_kwargs)

        _save_state(deploy_hash, body.image)
        return {"ok": True, "container_hash": deploy_hash}
    except docker.errors.ImageNotFound:
        _stop_and_remove(new_container)
        return {"ok": False, "error": "Image not found"}
    except docker.errors.APIError as e:
        _stop_and_remove(new_container)
        return {"ok": False, "error": str(e)}
    except Exception as e:
        _stop_and_remove(new_container)
        return {"ok": False, "error": str(e)}

@app.delete("/delete")
def delete() -> dict:
    """Delete the streambed container(s) managed by this daemon."""
    try:
        client = _get_docker()
        containers = client.containers.list(filters={"status": "running"})
        containers = [c for c in containers if c.name.startswith(f"streambed-{DEVICE_CLUSTER}-{DEVICE_ID}-")]
        if len(containers) == 0:
            return {"ok": False, "error": "No containers running"}
        for container in containers:
            _stop_and_remove(container.name)
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

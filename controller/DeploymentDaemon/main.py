"""StreamBed deployment daemon - pulls and runs containers from DockerHub."""
import asyncio
import hashlib
import json
import logging
import os
import secrets
from pathlib import Path

import platform
import docker
import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from contextlib import asynccontextmanager

from shared.bandwidth import SentRateBackend


# Configure logging (same format as controller)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

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

class DeployRequest(BaseModel):
    image: str
    host_port: int | None = None  # defaults to STREAMBED_HOST_PORT
    container_port: int | None = None  # defaults to STREAMBED_CONTAINER_PORT
    controller_url: str | None = None  # defaults to CONTROLLER_URL


class StreamTargetRequest(BaseModel):
    target_ip: str
    target_port: int


STATE_PATH = Path(__file__).parent / "data" / "deployed.json"
STREAM_TARGET_PATH = Path(__file__).parent / "data" / "stream-target.json"
DEFAULT_HOST_PORT = int(os.environ.get("STREAMBED_HOST_PORT", "8080"))
DEFAULT_CONTAINER_PORT = int(os.environ.get("STREAMBED_CONTAINER_PORT", "80"))

DAEMON_PORT = int(os.environ.get("DAEMON_PORT", "9090"))
DAEMON_ADDRESS = os.environ.get("DAEMON_ADDRESS", platform.node())

CONTROLLER_URL = os.environ.get("CONTROLLER_URL")
if not CONTROLLER_URL or not CONTROLLER_URL.strip():
    raise ValueError("CONTROLLER_URL is not set")

DEVICE_ID = os.environ.get("DEVICE_ID")
if not DEVICE_ID:
    raise ValueError("DEVICE_ID is not set")

DEVICE_CLUSTER = os.environ.get("DEVICE_CLUSTER")
if not DEVICE_CLUSTER:
    raise ValueError("DEVICE_CLUSTER is not set")

STREAM_PROXY_PORT = int(os.environ.get("STREAM_PROXY_PORT", "9000"))
STREAM_TARGET_POLL_INTERVAL = float(os.environ.get("STREAM_TARGET_POLL_INTERVAL", "2.0"))
BANDWIDTH_POLL_INTERVAL = float(os.environ.get("BANDWIDTH_POLL_INTERVAL", "1.0"))
# Memory limit for inference containers (edge/server) - PyTorch needs ~4–6GB
STREAMBED_MEMORY_LIMIT = os.environ.get("STREAMBED_MEMORY_LIMIT", "6g")


def _deployment_hash() -> str:
    """Generate a unique hash for this deployment."""
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()[:12]


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


class StreamProxyManager:
    """Singleton manager for stream proxy state (target, transport, protocol)."""

    _instance: "StreamProxyManager | None" = None

    def __new__(cls) -> "StreamProxyManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._target: dict[str, str | int | None] = {"ip": None, "port": None}
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: "StreamProxyProtocol | None" = None
        self._print_invalid_dest = False
        self._estimator: SentRateBackend | None = None
        self.target_bitrate: float | None = None

    def set_estimator(self, estimator: SentRateBackend) -> None:
        self._estimator = estimator

    def get_estimator(self) -> SentRateBackend | None:
        return self._estimator

    def update_estimator_bytes_sent(self, n: int) -> None:
        """Notify the estimator of bytes sent. No-op if no estimator; swallows errors."""
        try:
            self._estimator.on_bytes_sent(n)
        except Exception:
            pass

    def set_target(self, ip: str, port: int) -> None:
        self._target["ip"] = ip
        self._target["port"] = port
        self._print_invalid_dest = False

    def get_target(self) -> tuple[str | None, int | None]:
        return self._target["ip"], self._target["port"]

    def set_transport(self, transport: asyncio.DatagramTransport) -> None:
        self._transport = transport

    def set_protocol(self, protocol: "StreamProxyProtocol") -> None:
        self._protocol = protocol

    def invalid_logged(self) -> bool:
        return self._print_invalid_dest

    def mark_invalid_logged(self) -> None:
        self._print_invalid_dest = True

    def reset_invalid_logged(self) -> None:
        self._print_invalid_dest = False

    def close(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None
        self._protocol = None


class StreamProxyProtocol(asyncio.DatagramProtocol):
    """UDP proxy: receives on listen port, forwards to current stream-target."""

    def __init__(self, manager: StreamProxyManager):
        self.transport: asyncio.DatagramTransport | None = None
        self._manager = manager

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport
        self._manager.set_transport(transport)
        self._manager.set_protocol(self)

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        ip, port = self._manager.get_target()
        try:
            self.transport.sendto(data, (ip, port))
            self._manager.update_estimator_bytes_sent(len(data))
        except (OSError, TypeError):
            if not self._manager.invalid_logged():
                print("[Daemon] Invalid proxy target, dropping datagrams")
                self._manager.mark_invalid_logged()

    def error_received(self, exc: Exception) -> None:
        pass

    def connection_lost(self, exc: Exception | None) -> None:
        pass


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
            manager.target_bitrate = estimator.get_target_bps()
        await asyncio.sleep(BANDWIDTH_POLL_INTERVAL)


async def _run_stream_proxy(manager: StreamProxyManager) -> None:
    """Start UDP proxy on STREAM_PROXY_PORT. Only for edge daemons."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: StreamProxyProtocol(manager),
        local_addr=("0.0.0.0", STREAM_PROXY_PORT),
    )
    print(f"[Daemon] Stream proxy listening on 0.0.0.0:{STREAM_PROXY_PORT} (target from stream-target.json)")
    await asyncio.Future()  # run forever


_REGISTER_RETRIES = 5
_REGISTER_RETRY_DELAY = 2.0


async def _register_with_retries() -> None:
    url = f"{CONTROLLER_URL.rstrip('/')}/register"
    payload = {
        "device_cluster": DEVICE_CLUSTER,
        "device_id": DEVICE_ID,
        "ip": DAEMON_ADDRESS,
        "port": DAEMON_PORT
    }
    last_err: Exception | None = None
    for attempt in range(_REGISTER_RETRIES):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                return
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_err = e
            if attempt < _REGISTER_RETRIES - 1:
                await asyncio.sleep(_REGISTER_RETRY_DELAY)
    raise last_err


async def _deregister_with_retries() -> None:
    url = f"{CONTROLLER_URL.rstrip('/')}/deregister"
    payload = {
        "device_cluster": DEVICE_CLUSTER,
        "device_id": DEVICE_ID
    }
    last_err: Exception | None = None
    for attempt in range(_REGISTER_RETRIES):
        try:
            async with httpx.AsyncClient() as client:
                await client.delete(url, json=payload)
                return
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_err = e
            if attempt < _REGISTER_RETRIES - 1:
                await asyncio.sleep(_REGISTER_RETRY_DELAY)
    raise last_err


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _register_with_retries()

    poll_task = None
    proxy_task = None
    bandwidth_task = None
    stream_proxy_manager = StreamProxyManager()
    if DEVICE_ID.startswith("edge-"):
        stream_proxy_manager.set_estimator(SentRateBackend())
        poll_task = asyncio.create_task(_stream_proxy_target_poll_loop(stream_proxy_manager))
        proxy_task = asyncio.create_task(_run_stream_proxy(stream_proxy_manager))
        bandwidth_task = asyncio.create_task(_bandwidth_poll_loop(stream_proxy_manager))

    yield

    if bandwidth_task:
        bandwidth_task.cancel()
        try:
            await bandwidth_task
        except asyncio.CancelledError:
            pass
    if poll_task:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
    if proxy_task:
        proxy_task.cancel()
        try:
            await proxy_task
        except asyncio.CancelledError:
            pass
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
            old_container = f"streambed-{state['container_hash']}"
            _stop_and_remove(old_container)

        # 3. Run new container with port mapping, volumes, and memory limit (PyTorch needs ~1–2GB)
        run_kwargs = {
            "name": new_container,
            "detach": True,
            "ports": {f"{container_port}/tcp": host_port},
            "mem_limit": STREAMBED_MEMORY_LIMIT,
        }
        volumes = {}
        config_dir = os.environ.get("STREAMBED_CONFIG_HOST_PATH")
        if config_dir:
            volumes[config_dir] = {"bind": "/config", "mode": "ro"}
        data_dir = os.environ.get("STREAMBED_DATA_HOST_PATH")
        if data_dir:
            volumes[data_dir] = {"bind": "/data/streambed", "mode": "rw"}
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
        video_source = os.environ.get("VIDEO_SOURCE")
        if video_source:
            container_env["VIDEO_SOURCE"] = video_source
        # Edges send to daemon's stream proxy; pass host and port
        if DEVICE_ID.startswith("edge-"):
            proxy_host = os.environ.get("STREAM_PROXY_HOST", DAEMON_ADDRESS)
            proxy_port = int(os.environ.get("STREAM_PROXY_PORT", "9000"))
            container_env["STREAM_PROXY_HOST"] = proxy_host
            container_env["STREAM_PROXY_PORT"] = str(proxy_port)
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

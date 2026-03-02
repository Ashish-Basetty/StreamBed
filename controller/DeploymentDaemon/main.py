"""StreamBed deployment daemon - pulls and runs containers from DockerHub."""
import hashlib
import json
import os
import secrets
from pathlib import Path

import docker
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

_docker_client: docker.DockerClient | None = None


def _get_docker() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client

app = FastAPI(title="StreamBed Deployment Daemon")


class DeployRequest(BaseModel):
    image: str
    host_port: int | None = None  # defaults to STREAMBED_HOST_PORT
    container_port: int | None = None  # defaults to STREAMBED_CONTAINER_PORT


class StreamTargetRequest(BaseModel):
    target_ip: str
    target_port: int


STATE_PATH = Path(__file__).parent / "data" / "deployed.json"
STREAM_TARGET_PATH = Path(__file__).parent / "data" / "stream-target.json"
DEFAULT_HOST_PORT = int(os.environ.get("STREAMBED_HOST_PORT", "8080"))
DEFAULT_CONTAINER_PORT = int(os.environ.get("STREAMBED_CONTAINER_PORT", "80"))


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
    new_container = f"streambed-{deploy_hash}"

    try:
        client = _get_docker()

        # 1. Pull the new image (if this fails, old container stays running)
        client.images.pull(body.image)

        # 2. Stop old container to free the host port (can't have two containers on same port)
        state = _load_state()
        if state:
            old_container = f"streambed-{state['container_hash']}"
            _stop_and_remove(old_container)

        # 3. Run new container with port mapping (and config volume if host path provided)
        run_kwargs = {
            "name": new_container,
            "detach": True,
            "ports": {f"{container_port}/tcp": host_port},
        }
        data_dir = os.environ.get("STREAMBED_CONFIG_HOST_PATH")
        if data_dir:
            run_kwargs["volumes"] = {data_dir: {"bind": "/config", "mode": "ro"}}
        client.containers.run(body.image, **run_kwargs)

        _save_state(deploy_hash, body.image)
        return {"ok": True, "container_hash": deploy_hash}
    except docker.errors.ImageNotFound:
        _stop_and_remove(new_container)
        return {"ok": False, "error": "Image not found"}
    except docker.errors.APIError as e:
        _stop_and_remove(new_container)
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
    uvicorn.run(app, host="0.0.0.0", port=9090)

"""StreamBed controller node - SQLite-backed API server."""
from contextlib import asynccontextmanager

import os
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from db import get_connection, init_db, register_device, update_heartbeat
from deploy import DeployError, DeviceNotFoundError, deploy_to_device, delete_container_from_device
from heartbeat_spec import HeartbeatStatus


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="StreamBed Controller Node", lifespan=lifespan)


class HeartbeatRequest(BaseModel):
    device_cluster: str
    device_id: str
    current_model_version: str | None = None
    status: HeartbeatStatus | str | None = None


class RegisterRequest(BaseModel):
    device_cluster: str
    device_id: str
    ip: str | None = None  # override client address (e.g. hostname for testing)
    port: int | None = None  # override daemon port (default 9090)


class DeployRequest(BaseModel):
    device_cluster: str
    device_id: str
    image: str  # DockerHub image, e.g. "user/repo:tag"
    host_port: int | None = None  # defaults to daemon's STREAMBED_HOST_PORT
    container_port: int | None = None  # defaults to daemon's STREAMBED_CONTAINER_PORT

class DeleteRequest(BaseModel):
    device_cluster: str
    device_id: str


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/register")
def register_device_endpoint(request: Request, body: RegisterRequest) -> dict:
    """Register a device. IP/port from body if provided, else request client address."""
    ip = body.ip or (request.client.host if request.client else "0.0.0.0")
    port = body.port
    register_device(body.device_cluster, body.device_id, ip, port)
    return {"ok": True, "device_cluster": body.device_cluster, "device_id": body.device_id}

@app.post("/deregister")
def deregister_device_endpoint(request: Request, body: RegisterRequest) -> dict:
    """Deregister a device. IP/port from body if provided, else request client address."""
    deregister_device(body.device_cluster, body.device_id)
    return {"ok": True, "device_cluster": body.device_cluster, "device_id": body.device_id}


@app.get("/devices")
def list_devices(device_cluster: str) -> dict:
    """List registered devices in a cluster. device_cluster is required."""
    if not device_cluster.strip():
        raise HTTPException(status_code=400, detail="device_cluster is required")
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT device_cluster, device_id, ip, registered_at FROM devices WHERE device_cluster = ?",
            (device_cluster,),
        ).fetchall()
        return {"devices": [dict(row) for row in rows]}
    finally:
        conn.close()


@app.post("/deploy")
def deploy_container(body: DeployRequest) -> dict:
    """Deploy a container to a device. Forwards to the daemon and waits for success."""
    if not body.image.strip():
        raise HTTPException(status_code=400, detail="image is required")
    try:
        result = deploy_to_device(
            body.device_cluster,
            body.device_id,
            body.image,
            body.host_port,
            body.container_port,
            controller_url=os.environ.get("CONTROLLER_URL")
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except DeviceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except DeployError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

@app.delete("/delete")
def delete_container(body: DeleteRequest) -> dict:
    try:
        result = delete_container_from_device(
            body.device_cluster,
            body.device_id,
        )
        return result
    except DeviceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except DeployError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

@app.post("/heartbeat")
def receive_heartbeat(body: HeartbeatRequest) -> dict:
    """Record a heartbeat from a device. Status must be a valid HeartbeatStatus value."""
    try:
        update_heartbeat(
            body.device_cluster,
            body.device_id,
            body.current_model_version,
            body.status,
        )
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/status")
def list_status(device_cluster: str | None = None) -> dict:
    """List device status from heartbeats, optionally filtered by cluster."""
    conn = get_connection()
    try:
        if device_cluster:
            rows = conn.execute(
                """SELECT device_cluster, device_id, current_model, status, last_heartbeat
                   FROM device_status WHERE device_cluster = ?""",
                (device_cluster,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT device_cluster, device_id, current_model, status, last_heartbeat FROM device_status"
            ).fetchall()
        return {"status": [dict(row) for row in rows]}
    finally:
        conn.close()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)

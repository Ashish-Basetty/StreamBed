"""StreamBed controller node - SQLite-backed API server."""
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from db import get_connection, init_db, register_device, update_heartbeat
from deploy import DeployError, DeviceNotFoundError, deploy_to_device
from heartbeat_spec import HeartbeatStatus

app = FastAPI(title="StreamBed Controller Node")


class HeartbeatRequest(BaseModel):
    device_cluster: str
    device_id: str
    current_model_version: str | None = None
    status: HeartbeatStatus | str | None = None


class RegisterRequest(BaseModel):
    device_cluster: str
    device_id: str
    device_type: str  # e.g. "edge" or "server"
    current_model_version: str


class DeployRequest(BaseModel):
    device_cluster: str
    device_id: str
    image: str  # DockerHub image, e.g. "user/repo:tag"
    host_port: int | None = None  # defaults to daemon's STREAMBED_HOST_PORT
    container_port: int | None = None  # defaults to daemon's STREAMBED_CONTAINER_PORT


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/register")
def register_device_endpoint(request: Request, body: RegisterRequest) -> dict:
    """Register a device. IP is taken from the request's client address."""
    ip = request.client.host if request.client else "0.0.0.0"
    register_device(body.device_cluster, body.device_id, ip, body.device_type)
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

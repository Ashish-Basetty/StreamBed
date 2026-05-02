"""StreamBed controller node - SQLite-backed API server."""
from contextlib import asynccontextmanager

import logging
import os
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from db import get_connection, init_db, register_device, update_heartbeat
from deploy import DeployError, DeviceNotFoundError, deploy_to_device, delete_container_from_device
from health_monitor import create_and_start_monitor, HealthMonitor
from shared.interfaces.heartbeat_spec import HeartbeatStatus

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Health monitor instance (started in lifespan)
health_monitor: HealthMonitor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):

    global health_monitor
    init_db()

    # Initialize routing table: assign each edge to the first available server in its cluster if not already present
    conn = get_connection()
    try:
        clusters = conn.execute("SELECT DISTINCT device_cluster FROM devices").fetchall()
        for row in clusters:
            cluster = row[0]
            devices = conn.execute(
                "SELECT device_id, device_type FROM devices WHERE device_cluster=?",
                (cluster,),
            ).fetchall()
            servers = [d[0] for d in devices if d[1] == 'server']
            edges = [d[0] for d in devices if d[1] == 'edge']
            if not servers:
                continue
            target_server = servers[0]
            for edge in edges:
                exists = conn.execute(
                    "SELECT 1 FROM routing WHERE source_cluster=? AND source_device=?",
                    (cluster, edge)
                ).fetchone()
                if not exists:
                    conn.execute(
                        """
                        INSERT INTO routing (source_cluster, source_device, target_cluster, target_device, updated_at)
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (cluster, edge, cluster, target_server)
                    )
                    logger.info(f"[INIT] Routing: {cluster}/{edge} -> {target_server}")
        conn.commit()
    except Exception as e:
        logger.error(f"[INIT] Error initializing routing table: {e}")
    finally:
        conn.close()

    # Start health monitoring
    heartbeat_timeout = int(os.environ.get("HEARTBEAT_TIMEOUT_SECS", "30"))
    check_interval = int(os.environ.get("HEALTH_CHECK_INTERVAL_SECS", "5"))
    controller_url = os.environ.get("CONTROLLER_URL")
    health_monitor = await create_and_start_monitor(
        heartbeat_timeout_secs=heartbeat_timeout,
        check_interval_secs=check_interval,
        controller_url=controller_url,
    )
    logger.info(
        f"Health monitor started (timeout={heartbeat_timeout}s, interval={check_interval}s)"
    )

    yield

    # Stop health monitoring on shutdown
    if health_monitor:
        await health_monitor.stop()



app = FastAPI(title="StreamBed Controller Node", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class HeartbeatRequest(BaseModel):
    device_cluster: str
    device_id: str
    current_model_version: str | None = None
    status: HeartbeatStatus | str | None = None


class RegisterRequest(BaseModel):
    device_cluster: str
    device_id: str
    device_type: str  # "edge" or "server"
    ip: str | None = None  # override client address (e.g. hostname for testing)
    port: int | None = None  # override daemon port (default 9090)


class DeployRequest(BaseModel):
    device_cluster: str
    device_id: str
    device_type: str  # "edge" or "server"
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
    register_device(body.device_cluster, body.device_id, body.device_type, ip, port)
    if body.device_type == "edge":
        _assign_edge_to_least_loaded_server(body.device_cluster, body.device_id)
    elif body.device_type == "server":
        _assign_unrouted_edges(body.device_cluster)
    return {"ok": True, "device_cluster": body.device_cluster, "device_id": body.device_id}


def _assign_edge_to_least_loaded_server(cluster: str, edge_id: str) -> None:
    """Assign a new edge to the server in the cluster with the fewest incoming routes."""
    conn = get_connection()
    try:
        servers = [
            r[0] for r in conn.execute(
                "SELECT device_id FROM devices WHERE device_cluster=? AND device_type='server'",
                (cluster,),
            ).fetchall()
        ]
        if not servers:
            return

        # Count existing routes per server
        load = {s: 0 for s in servers}
        rows = conn.execute(
            "SELECT target_device, COUNT(*) FROM routing WHERE source_cluster=? GROUP BY target_device",
            (cluster,),
        ).fetchall()
        for target, count in rows:
            if target in load:
                load[target] = count

        target_server = min(load, key=load.get)

        conn.execute(
            """INSERT INTO routing (source_cluster, source_device, target_cluster, target_device, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(source_cluster, source_device) DO UPDATE SET
                   target_cluster=excluded.target_cluster,
                   target_device=excluded.target_device,
                   updated_at=CURRENT_TIMESTAMP""",
            (cluster, edge_id, cluster, target_server),
        )
        conn.commit()
        logger.info(f"[REGISTER] Routed {cluster}/{edge_id} -> {target_server} (load: {load})")
    except Exception as e:
        logger.error(f"[REGISTER] Error assigning route for {edge_id}: {e}")
    finally:
        conn.close()

def _assign_unrouted_edges(cluster: str) -> None:
    """When a server registers, assign any edges in the cluster that have no routing entry."""
    conn = get_connection()
    try:
        edges = [
            r[0] for r in conn.execute(
                "SELECT device_id FROM devices WHERE device_cluster=? AND device_type='edge'",
                (cluster,),
            ).fetchall()
        ]
        routed = {
            r[0] for r in conn.execute(
                "SELECT source_device FROM routing WHERE source_cluster=?",
                (cluster,),
            ).fetchall()
        }
        unrouted = [e for e in edges if e not in routed]
    finally:
        conn.close()

    for edge_id in unrouted:
        _assign_edge_to_least_loaded_server(cluster, edge_id)


@app.post("/deregister")
def deregister_device_endpoint(request: Request, body: RegisterRequest) -> dict:
    """Deregister a device. IP/port from body if provided, else request client address."""
    deregister_device(body.device_cluster, body.device_id)
    return {"ok": True, "device_cluster": body.device_cluster, "device_id": body.device_id}


@app.get("/clusters")
def list_clusters() -> dict:
    """List all distinct cluster names that have at least one registered device."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT DISTINCT device_cluster FROM devices ORDER BY device_cluster").fetchall()
        return {"clusters": [row["device_cluster"] for row in rows]}
    finally:
        conn.close()


@app.get("/devices")
def list_devices(device_cluster: str) -> dict:
    """List registered devices in a cluster. device_cluster is required."""
    if not device_cluster.strip():
        raise HTTPException(status_code=400, detail="device_cluster is required")
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT device_cluster, device_id, device_type, ip, registered_at FROM devices WHERE device_cluster = ?",
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
            body.device_type,
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


@app.get("/routing")
def list_routing(device_cluster: str | None = None) -> dict:
    """List all routing table entries, optionally filtered by source cluster."""
    conn = get_connection()
    try:
        if device_cluster:
            rows = conn.execute(
                "SELECT source_cluster, source_device, target_cluster, target_device, updated_at FROM routing WHERE source_cluster = ?",
                (device_cluster,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT source_cluster, source_device, target_cluster, target_device, updated_at FROM routing"
            ).fetchall()
        return {"routing": [dict(row) for row in rows]}
    finally:
        conn.close()


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


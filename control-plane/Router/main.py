"""StreamBed request router — FastAPI proxy with SQLite-backed cluster→controller table."""
import logging
import os
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from admin import router as admin_router
from proxy import fanout_clusters, forward

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Shared httpx client lives across the lifespan; avoids per-request connection setup.
_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    db.init_db()

    # Backfill the routing table if empty — same pattern as controller's
    # lifespan-time `routing` table init.
    default_cluster = os.environ.get("ROUTER_DEFAULT_CLUSTER", "default")
    default_controller = os.environ.get(
        "ROUTER_DEFAULT_CONTROLLER_URL", "http://controller:8080"
    )
    if db.seed_if_empty(default_cluster, default_controller):
        logger.info(
            f"[INIT] Seeded empty routing table: {default_cluster} -> {default_controller}"
        )

    _client = httpx.AsyncClient(timeout=30)
    logger.info("Router started")
    yield
    if _client:
        await _client.aclose()


app = FastAPI(title="StreamBed Router", lifespan=lifespan)
app.include_router(admin_router)


# --- Body schemas matching the controller's, used to pull cluster from JSON ---


class _ClusterCarryingBody(BaseModel):
    device_cluster: str
    # other fields ignored at the router layer; controller revalidates


def _cluster_from_query(request: Request) -> str:
    cluster = request.query_params.get("device_cluster")
    if not cluster:
        raise HTTPException(400, "device_cluster query param is required")
    return cluster


async def _cluster_from_body(request: Request) -> str:
    """Parse JSON body once, pull device_cluster. Caller must NOT call request.body() again
    after this — instead, the proxy uses request.body() which is cached after first read."""
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(400, f"Invalid JSON body: {e}")
    cluster = payload.get("device_cluster") if isinstance(payload, dict) else None
    if not cluster:
        raise HTTPException(400, "device_cluster is required in body")
    return cluster


# --- Health ---


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# --- Fan-out endpoints ---


@app.get("/clusters")
async def list_clusters_fanout() -> dict:
    assert _client is not None
    return {"clusters": await fanout_clusters(_client)}


# --- Query-param-routed proxies ---


@app.get("/devices")
async def proxy_devices(request: Request):
    assert _client is not None
    return await forward(request, _cluster_from_query(request), _client)


@app.get("/routing")
async def proxy_routing(request: Request):
    assert _client is not None
    return await forward(request, _cluster_from_query(request), _client)


@app.get("/status")
async def proxy_status(request: Request):
    assert _client is not None
    return await forward(request, _cluster_from_query(request), _client)


@app.get("/deployments")
async def proxy_deployments(request: Request):
    assert _client is not None
    return await forward(request, _cluster_from_query(request), _client)


# --- Body-routed proxies ---


@app.post("/register")
async def proxy_register(request: Request):
    assert _client is not None
    return await forward(request, await _cluster_from_body(request), _client)


@app.post("/deregister")
async def proxy_deregister(request: Request):
    assert _client is not None
    return await forward(request, await _cluster_from_body(request), _client)


@app.post("/heartbeat")
async def proxy_heartbeat(request: Request):
    assert _client is not None
    return await forward(request, await _cluster_from_body(request), _client)


@app.post("/deploy")
async def proxy_deploy(request: Request):
    assert _client is not None
    return await forward(request, await _cluster_from_body(request), _client)


@app.delete("/delete")
async def proxy_delete(request: Request):
    assert _client is not None
    return await forward(request, await _cluster_from_body(request), _client)


# --- Static frontend at / (must be mounted last so routes take precedence) ---


_FRONTEND_DIR = os.environ.get("FRONTEND_DIR", "/app/frontend")
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
else:
    logger.info(f"Frontend dir {_FRONTEND_DIR} not present; skipping static mount")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ROUTER_PORT", "8090")))

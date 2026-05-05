"""Admin endpoints — the only writers to cluster_routing."""
import os
import secrets

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import db

router = APIRouter(prefix="/admin", tags=["admin"])


class RouteUpsert(BaseModel):
    cluster_name: str
    controller_url: str


def _check_token(x_router_admin_token: str | None) -> None:
    expected = os.environ.get("ROUTER_ADMIN_TOKEN", "").strip()
    if not expected:
        raise HTTPException(503, "Router admin disabled (ROUTER_ADMIN_TOKEN not set)")
    if not x_router_admin_token:
        raise HTTPException(401, "Missing X-Router-Admin-Token header")
    if not secrets.compare_digest(x_router_admin_token, expected):
        raise HTTPException(401, "Invalid admin token")


@router.get("/clusters")
def list_clusters() -> dict:
    """Inspect the routing table. Read-only — no token required."""
    return {"routes": db.list_routes()}


@router.post("/clusters")
def upsert_cluster(
    body: RouteUpsert,
    x_router_admin_token: str | None = Header(default=None),
) -> dict:
    _check_token(x_router_admin_token)
    if not body.cluster_name.strip() or not body.controller_url.strip():
        raise HTTPException(400, "cluster_name and controller_url are required")
    db.upsert_route(body.cluster_name, body.controller_url)
    return {"ok": True, "cluster_name": body.cluster_name, "controller_url": body.controller_url}


@router.delete("/clusters/{cluster_name}")
def delete_cluster(
    cluster_name: str,
    x_router_admin_token: str | None = Header(default=None),
) -> dict:
    _check_token(x_router_admin_token)
    deleted = db.delete_route(cluster_name)
    if not deleted:
        raise HTTPException(404, f"No route for cluster '{cluster_name}'")
    return {"ok": True, "cluster_name": cluster_name}

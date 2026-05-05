"""Router unit tests. Stubs controllers via httpx.MockTransport — no live network."""
import os
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

# Make the router module importable directly.
ROUTER_DIR = Path(__file__).resolve().parents[1] / "controller" / "Router"
sys.path.insert(0, str(ROUTER_DIR))


@pytest.fixture
def router_app(monkeypatch, tmp_path):
    """Spin up the Router FastAPI app with a temp DB and a stubbed httpx transport.
    Returns (app, db_module, set_transport(callable)) so tests can wire up controllers.
    """
    db_path = tmp_path / "router.db"
    monkeypatch.setenv("ROUTER_DB_PATH", str(db_path))
    monkeypatch.setenv("ROUTER_ADMIN_TOKEN", "test-token")
    monkeypatch.setenv("FRONTEND_DIR", "/nonexistent")  # skip static mount

    # Reload modules picking up the env vars.
    for mod in ("db", "proxy", "admin", "main"):
        sys.modules.pop(mod, None)

    import db  # type: ignore
    db.init_db()

    import main  # type: ignore
    app = main.app

    return app, db, main


def _make_handler(routes):
    """routes: dict of (method, path) -> (status, json_body) or callable returning same."""
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key not in routes:
            return httpx.Response(404, json={"detail": f"unmocked: {key}"})
        v = routes[key]
        if callable(v):
            return v(request)
        status, body = v
        return httpx.Response(status, json=body)
    return handler


def _install_transport(main_module, handler):
    """Replace the router's shared httpx client with one using a MockTransport."""
    main_module._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_proxy_routes_by_query_param(router_app):
    app, db, main = router_app
    db.upsert_route("alpha", "http://ctrl-a:8080")
    db.upsert_route("beta", "http://ctrl-b:8080")

    seen = []
    def handler(req):
        seen.append((req.url.host, req.url.path, dict(req.url.params)))
        return httpx.Response(200, json={"devices": []})
    _install_transport(main, handler)

    client = TestClient(app)
    r = client.get("/devices", params={"device_cluster": "alpha"})
    assert r.status_code == 200
    assert seen[0][0] == "ctrl-a"
    assert seen[0][2]["device_cluster"] == "alpha"

    r = client.get("/devices", params={"device_cluster": "beta"})
    assert seen[1][0] == "ctrl-b"


def test_proxy_unknown_cluster_returns_404(router_app):
    app, _, main = router_app
    _install_transport(main, _make_handler({}))
    client = TestClient(app)
    r = client.get("/devices", params={"device_cluster": "nonexistent"})
    assert r.status_code == 404


def test_proxy_unreachable_controller_returns_502(router_app):
    app, db, main = router_app
    db.upsert_route("alpha", "http://ctrl-a:8080")

    def handler(req):
        raise httpx.ConnectError("nope", request=req)
    _install_transport(main, handler)

    client = TestClient(app)
    r = client.get("/devices", params={"device_cluster": "alpha"})
    assert r.status_code == 502


def test_proxy_routes_post_by_body(router_app):
    app, db, main = router_app
    db.upsert_route("alpha", "http://ctrl-a:8080")
    db.upsert_route("beta", "http://ctrl-b:8080")

    seen = []
    def handler(req):
        seen.append((req.url.host, req.url.path))
        return httpx.Response(200, json={"ok": True})
    _install_transport(main, handler)

    client = TestClient(app)
    r = client.post(
        "/deploy",
        json={"device_cluster": "beta", "device_id": "x", "device_type": "edge", "image": "img:1"},
    )
    assert r.status_code == 200
    assert seen[0][0] == "ctrl-b"


def test_fanout_clusters_merges_across_controllers(router_app):
    app, db, main = router_app
    db.upsert_route("alpha", "http://ctrl-a:8080")
    db.upsert_route("beta", "http://ctrl-b:8080")

    def handler(req):
        if req.url.host == "ctrl-a":
            return httpx.Response(200, json={"clusters": ["alpha", "shared"]})
        if req.url.host == "ctrl-b":
            return httpx.Response(200, json={"clusters": ["beta", "shared"]})
        return httpx.Response(404)
    _install_transport(main, handler)

    client = TestClient(app)
    r = client.get("/clusters")
    assert r.status_code == 200
    assert r.json()["clusters"] == ["alpha", "beta", "shared"]


def test_admin_requires_token(router_app):
    app, _, main = router_app
    _install_transport(main, _make_handler({}))
    client = TestClient(app)

    body = {"cluster_name": "alpha", "controller_url": "http://ctrl-a:8080"}

    # No token
    r = client.post("/admin/clusters", json=body)
    assert r.status_code == 401

    # Wrong token
    r = client.post("/admin/clusters", json=body, headers={"X-Router-Admin-Token": "wrong"})
    assert r.status_code == 401

    # Right token
    r = client.post("/admin/clusters", json=body, headers={"X-Router-Admin-Token": "test-token"})
    assert r.status_code == 200


def test_lifespan_seeds_when_empty(router_app):
    app, db, main = router_app
    # Fixture's init_db already ran; routing table is empty before lifespan.
    # TestClient triggers lifespan when used as a context manager.
    _install_transport(main, _make_handler({}))
    with TestClient(app) as client:
        r = client.get("/admin/clusters")
    assert r.status_code == 200
    routes = r.json()["routes"]
    assert len(routes) == 1
    assert routes[0]["cluster_name"] == "default"
    assert routes[0]["controller_url"] == "http://controller:8080"


def test_lifespan_does_not_overwrite_existing(router_app):
    app, db, main = router_app
    db.upsert_route("custom", "http://ctrl-x:9999")
    _install_transport(main, _make_handler({}))
    with TestClient(app) as client:
        r = client.get("/admin/clusters")
    routes = r.json()["routes"]
    names = {row["cluster_name"] for row in routes}
    assert names == {"custom"}  # default not auto-seeded; table wasn't empty


def test_admin_upsert_then_list(router_app):
    app, _, main = router_app
    _install_transport(main, _make_handler({}))
    client = TestClient(app)
    headers = {"X-Router-Admin-Token": "test-token"}

    client.post(
        "/admin/clusters",
        json={"cluster_name": "alpha", "controller_url": "http://ctrl-a:8080"},
        headers=headers,
    )
    client.post(
        "/admin/clusters",
        json={"cluster_name": "beta", "controller_url": "http://ctrl-b:8080"},
        headers=headers,
    )

    r = client.get("/admin/clusters")
    assert r.status_code == 200
    names = {row["cluster_name"] for row in r.json()["routes"]}
    assert names == {"alpha", "beta"}

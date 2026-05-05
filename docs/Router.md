# Router Plan

The Router is a public-facing service that takes incoming HTTP requests and forwards them to the right controller based on which cluster the request is for. It also serves the dashboard.

This is the load-balancing/routing-at-scale piece deferred from earlier docs ([GCPTestInfra.md](GCPTestInfra.md), [NginxControllerWrap.md](NginxControllerWrap.md)). It does **not** do load balancing across replicas of one controller — only fan-out across distinct controllers that own different clusters. Controller HA is a later concern.

## Topology and ownership rules

```
                   Internet
                       │
                       ▼  (TLS)
              ┌────────────────┐
              │     Caddy      │   ← this VM's only public port
              └────────┬───────┘
                       │ http://router:8090
                       ▼
              ┌────────────────┐
              │  Router (Py)   │
              │  FastAPI +     │
              │  SQLite        │
              └────────┬───────┘
              ┌────────┼────────────┐
              ▼        ▼            ▼
           ctrl-A   ctrl-B  …    (N controllers, internal VPC only)
              │       │
       cluster X    clusters Y, Z
```

**Ownership invariant:** every cluster is owned by **exactly one** controller. A controller can own **any number** of clusters. The mapping is `cluster_name → controller_url`, many-to-one.

For now (test infra): one controller, one cluster (`default`), but the schema and code path assume the general case from day one so scaling out is just "insert another row in the routing table and stand up another controller."

## Data model

A new SQLite database, separate from the controller's: `router/data/router.db`.

```sql
CREATE TABLE IF NOT EXISTS cluster_routing (
    cluster_name   TEXT PRIMARY KEY,
    controller_url TEXT NOT NULL,         -- e.g. http://controller-01:8080
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

That's the entire schema. PK on `cluster_name` enforces the "one controller per cluster" invariant: two writers can't disagree.

### Concurrency: one writer, many readers

SQLite is fundamentally one-writer / many-reader. Two things make it work cleanly here:

1. **Enable WAL mode.** `PRAGMA journal_mode=WAL` lets multiple readers proceed concurrently with each other and with a single writer, no readlock blocking. Set it once on db init.
2. **Funnel all writes through one code path.** The admin handlers (§"HTTP surface" below) are the only writers. Proxy handlers are read-only. Don't sprinkle `INSERT`s elsewhere.

In practice the table is tiny (one row per cluster) and rarely changes — writes happen when an operator adds/removes a controller, maybe a few times a week. Reads happen on every proxied request. Read-heavy is exactly what SQLite is good at.

In-memory caching is **not** needed for v1. The DB read on each request is sub-millisecond at this scale. Add a TTL cache only if you measure it as a bottleneck (you won't).

## HTTP surface

Three categories of endpoint.

### A. Proxied — pass through to the right controller

These all carry `device_cluster` either in the query string or the JSON body. The router parses it, looks up `controller_url`, forwards the request verbatim, returns the response unchanged.

| controller endpoint | how to find cluster |
| --- | --- |
| `GET /devices?device_cluster=X` | query param |
| `GET /routing?device_cluster=X` | query param (optional in controller; required by router) |
| `GET /deployments?device_cluster=X` | query param |
| `GET /status?device_cluster=X` | query param |
| `POST /register` | body |
| `POST /deregister` | body |
| `POST /heartbeat` | body |
| `POST /deploy` | body |
| `DELETE /delete` | body |

Proxy is a thin async wrapper over `httpx`. Fail-fast: if `cluster` not in the table → 404. If the controller URL is unreachable → 502.

### B. Fan-out — query every controller, merge

These don't carry a cluster scope; they're meant to span all clusters.

| endpoint | strategy |
| --- | --- |
| `GET /clusters` | call `GET /clusters` on every controller in the table, dedupe + sort. (Sanity-check: assert the controller actually owns the clusters it claims — if `controller_url` X says it has cluster `foo` but the routing table says `foo` belongs to controller Y, log a warning and trust the table.) |

If you find yourself adding many fan-out endpoints, that's a smell — most cluster-scoped operations should have a `device_cluster` filter on the controller side too.

### C. Admin — writes the routing table

These are the **single writer** to `cluster_routing`. Gate them behind a static admin token (env var `ROUTER_ADMIN_TOKEN`). Comparing strings in Python with `secrets.compare_digest` to avoid timing leaks. Real auth (OIDC, mTLS) is future work.

| endpoint | body | semantics |
| --- | --- | --- |
| `POST /admin/clusters` | `{cluster_name, controller_url}` | UPSERT — register or remap a cluster |
| `DELETE /admin/clusters/{cluster_name}` | — | remove a cluster from routing |
| `GET /admin/clusters` | — | list rows in `cluster_routing` (for diagnostics; can also be public if you want) |

### D. Static frontend

The router serves the existing dashboard from `frontend/`. Same pattern as we explored for the controller: FastAPI `StaticFiles` mount at `/`, declared after all API routes so route precedence works.

Frontend's `app.js` already calls relative URLs (`/clusters`, `/devices?...`), so it'll Just Work pointed at the router.

## Lookup logic — the one function the router actually does

```python
async def proxy(request, cluster_name):
    row = db.execute(
        "SELECT controller_url FROM cluster_routing WHERE cluster_name=?",
        (cluster_name,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"No controller for cluster '{cluster_name}'")
    target = row[0]
    forwarded = await client.request(
        request.method,
        f"{target}{request.url.path}",
        params=request.query_params,
        content=await request.body(),
        headers={k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length"}},
        timeout=30,
    )
    return Response(
        content=forwarded.content,
        status_code=forwarded.status_code,
        headers=dict(forwarded.headers),
    )
```

The dispatch table mapping FastAPI route → "extract cluster from query" / "extract cluster from JSON body" is small enough to enumerate by hand. Don't try to be clever with reflection.

## File layout

```
controller/Router/
├── Dockerfile               (already exists; will be expanded)
├── main.py                  ← FastAPI app, lifespan, mount static, register routes
├── requirements.txt         ← fastapi, uvicorn, httpx
├── db.py                    ← schema init, WAL pragma, get_connection
├── proxy.py                 ← proxy() helper + per-endpoint dispatchers
├── admin.py                 ← admin token check + write endpoints
└── data/                    ← SQLite db lives here (volume mount)
```

Roughly ~250 lines of Python total. Smaller than the controller itself.

## Caddy

Same pattern proposed for the controller, applied to the router instead. The Router VM (or whichever VM the router lives on) runs a Caddy container in front:

```caddyfile
{$CADDY_DOMAIN} {
    encode gzip
    reverse_proxy router:8090
    tls {$CADDY_EMAIL}
}
```

Open question: should the router spawn its own Caddy (the controller-spawns-Caddy pattern we briefly considered then rolled back), or should Caddy be a sibling docker-compose service? Given this is a fresh component without legacy patterns, **sibling docker-compose service** is the right call here — same arguments as in [NginxControllerWrap.md](NginxControllerWrap.md) "What I'd do differently": cleaner restart story, no docker.sock, no cold-start gap.

## Deployment options

Two reasonable shapes for the GCP test infra:

### Option 1: dedicated Router VM (cleaner)

- Add a 6th VM to [infra/gcp/vms.tf](../infra/gcp/vms.tf): `router-01`, `e2-micro`.
- Router VM is the **only** VM with an external IP. Controller(s) become internal-only.
- Firewall rule `allow-controller-http` gets retargeted from `controller` tag to `router` tag.
- Frontend dashboard is hit at the router's domain, not the controller's.

Cost: +$6/month always-on, +$0 burst-only. Recommended.

### Option 2: router and controller co-located on one VM (cheaper for v1)

- Router and controller-01 share a VM. Router is internet-facing on 8080 (or 443 via Caddy); controller listens only on 127.0.0.1.
- Saves a VM, but couples failure domains: if that VM dies, you lose the router and one controller (the entire cluster).
- Reasonable for a single-controller test stack. Awkward when you scale to N controllers — at that point Option 1 is the only sensible layout.

For now (single controller, single cluster, $50 budget): **Option 2** is fine. Migrate to Option 1 the moment you stand up a second controller.

## Migration path: how the existing dashboard flips over

1. Bring the router up *next to* the controller (Option 2 deployment), pointing at `http://localhost:8080`.
2. Seed the routing table: `POST /admin/clusters {cluster_name: "default", controller_url: "http://localhost:8080"}`.
3. Update the dashboard URL to point at the router (`https://router.example.com/`). Frontend's `API: ''` already uses relative URLs, so no code change in app.js.
4. Once the router is validated, lock down the controller: drop port 8080 from the GCP firewall (router talks to controller via VPC internal IP).
5. The frontend now works against the router. From here, adding a second controller is "boot a new controller VM, `POST /admin/clusters {cluster_name: 'team-b', controller_url: 'http://controller-02:8080'}`."

## What's deliberately out of scope

- **Real auth** between dashboard and router. Static admin token only. Real users + RBAC come later.
- **mTLS** between router and controllers. Trust the VPC for now.
- **Controller HA / failover.** One controller per cluster, no replicas. If a controller dies, that cluster is down until it comes back.
- **Auto-discovery / heartbeats from controllers.** Manual admin POSTs only.
- **Quorum / consensus on the routing table.** SQLite on one VM is the source of truth. If the router VM dies, the routing table dies with it (until restore).
- **Cross-region.** Single region.
- **Rate limiting beyond what Caddy gives for free.** Add `rate_limit` block in Caddyfile when needed.

These are all worth tackling once the basic shape is alive and you've found which limitations actually bite. Don't pre-build any of them.

## Testing

Add `tests/test_router.py` with at minimum:

1. Seed two fake controller URLs in the routing table → calls to `/devices?device_cluster=A` go to controller A, calls with `device_cluster=B` go to controller B.
2. Unknown cluster → 404.
3. Controller URL unreachable → 502.
4. `POST /admin/clusters` without token → 401. With wrong token → 401. With right token → 200.
5. `GET /clusters` fans out across both controllers and merges.

Use `httpx.MockTransport` to stub controllers — no need for real HTTP servers in tests.

## Decisions (v1)

Resolved with defaults so we can move; flag any you want to revisit:

1. **Router location**: Option 2 (co-located with controller-01 on the same VM via docker-compose). Saves a VM. Migrate to Option 1 the moment you stand up a second controller.
2. **Admin auth**: static `ROUTER_ADMIN_TOKEN` env var, compared with `secrets.compare_digest`. Real auth comes when device auth lands — same scope.
3. **Proxy timeout**: 30s default, no per-endpoint overrides yet. `/deploy` is the slowest endpoint (image pulls); 30s has been fine in local testing. Bump to 60s if you start seeing timeouts on cold-pull deploys.
4. **In-memory cache**: no. Re-evaluate only if a profiler points at the SQLite read.

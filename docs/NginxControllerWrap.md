# Wrapping the Controller with nginx

Goal: put nginx in front of the controller's FastAPI ([controller/ControllerNode](../controller/ControllerNode)) on the GCP test VM. Today the controller listens on port 8080 directly. After this change:

- Public traffic hits **port 80/443** on the controller VM.
- nginx terminates TLS (Let's Encrypt), serves the static frontend ([frontend/](../frontend)), and proxies `/api/*` to the FastAPI app on `127.0.0.1:8080`.
- The FastAPI app stops being directly internet-exposed.

What nginx is **not** doing here: load balancing across multiple controllers. That's the separate horizontal-scaling router work and lives outside this doc.

## What we want from the wrapper

Concretely:

1. **TLS termination** with auto-renewing Let's Encrypt cert.
2. **Static frontend** served from same origin so the dashboard's existing `fetch('/clusters')` works without CORS hacks (and the frontend's `API: 'http://localhost:8080'` becomes `API: ''` — same-origin).
3. **Single port** (443) instead of remembering `:8080`.
4. **Basic gating** — IP allowlist or HTTP basic auth on write endpoints (`POST /deploy`, `DELETE /delete`, etc.). Read endpoints stay open.
5. **Reasonable defaults**: gzip, sane request size limits, access log.

Cert renewal must be unattended — leaving a self-signed cert or a forgotten manual renewal in a test VM is how this kind of thing rots.

## Alternatives

Five realistic options. Trade-offs are about complexity, who owns cert renewal, and how coupled the lifecycles are.

### A. Sibling nginx container in `docker-compose.yml` (recommended)

Add a service to the existing [docker-compose.yml](../docker-compose.yml):

```yaml
nginx:
  image: nginx:1.27-alpine
  container_name: streambed-nginx
  ports: ["80:80", "443:443"]
  volumes:
    - ./controller/nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro
    - ./frontend:/usr/share/nginx/html:ro
    - ./controller/nginx/letsencrypt:/etc/letsencrypt
  depends_on: [controller]
  networks: [streambed-net]
  restart: unless-stopped
```

Plus a one-shot `certbot` invocation (or sibling `certbot/certbot` container on a cron) that writes into the shared `letsencrypt` volume. nginx reloads on cert change.

The controller's `ports: ["8080:8080"]` block changes to `expose: ["8080"]` — internal only; nginx talks to it over the docker network at `http://controller:8080`.

**Pros**

- Plays nicely with what's already there. One `docker compose up` brings everything.
- One process per container. Independent restarts; nginx config typo doesn't crash the controller.
- Trivial to swap images later (Caddy, OpenResty) without touching FastAPI.
- nginx config is a normal file in the repo, version-controlled.

**Cons**

- You write the nginx config (~30 lines for what we need).
- You wire up certbot yourself. Two common patterns: (1) sibling `certbot/certbot` container run via a `crontab` on the host (cheapest), or (2) `--manual` once and never renew (don't).

**Cost**: zero beyond what you're already paying.

### B. Prebuilt `jonasal/nginx-certbot` image

An off-the-shelf image that bundles nginx + certbot + a script that requests certs on first start and reloads nginx on renewal. Same compose shape as A, but the certbot wiring is done for you.

**Pros**

- Less yak-shaving on cert renewal. First-boot is roughly: set `CERTBOT_EMAIL`, list domains, `docker compose up`.
- Maintained image; gets security updates.

**Cons**

- An extra dependency on a third-party image. If the maintainer disappears you're forking.
- Slightly more magic — when something breaks at 3 a.m. you have to read someone else's startup script to figure out why.
- Same daily ops surface as A; you only saved the initial certbot wiring.

If you want zero-effort certs and don't mind the dependency, this is a fine choice. If you want to understand every moving piece, do A.

### C. Caddy in a sibling container (simplest config, auto-HTTPS)

Replace nginx with [Caddy](https://caddyserver.com/). Caddy has Let's Encrypt baked in and renews automatically. The Caddyfile for our use case is ~6 lines:

```caddyfile
streambed.example.com {
    encode gzip
    handle /api/* {
        uri strip_prefix /api
        reverse_proxy controller:8080
    }
    root * /usr/share/caddy
    file_server
}
```

Compose service is similar to A but with `image: caddy:2-alpine` and a `caddy_data` volume for cert state.

**Pros**

- Smallest config of any option.
- TLS is fully automatic — first request triggers cert issuance, renewals are silent.
- Sensible defaults (HSTS, OCSP stapling, modern ciphers).

**Cons**

- It's not nginx. If the project standardizes on nginx for the future router system (or if you've explicitly decided "I want nginx skills"), this is wasted muscle memory.
- Smaller community than nginx — fewer Stack Overflow hits when something obscure breaks.

If your reason for choosing nginx is "I need to learn nginx," skip this. If you just want HTTPS and a reverse proxy with the least possible config, Caddy is objectively easier.

### D. Bake nginx into the controller image (single container)

Modify [controller/ControllerNode/Dockerfile](../controller/ControllerNode/Dockerfile) to install nginx alongside Python, and use `supervisord` or `s6-overlay` to run both processes inside one container.

**Pros**

- One container to manage. No compose service to add.
- If you're deploying to a platform that only lets you run a single container per app, this is the only option.

**Cons**

- Violates "one process per container." Surprises are larger: an nginx config error crashes the whole controller.
- Image gets bigger and slower to build.
- Reload of nginx requires a process supervisor inside the container — extra moving parts.
- Cert renewal still needs solving and is now happening *inside* the controller container, which complicates restarts.
- Hard to swap to Caddy or anything else later — you'd be unbuilding the image.

I'd avoid this. It only makes sense if external constraints force a single-container deployment, which they don't here.

### E. nginx running on the host VM (not in Docker)

Install nginx via `apt` on the controller VM, configure it to proxy `127.0.0.1:8080`. The controller container still runs in Docker.

**Pros**

- nginx runs as a normal systemd service. Boring and stable.
- `apt-get install certbot python3-certbot-nginx` and `certbot --nginx` does end-to-end TLS in one command, with renewal as a system cron.

**Cons**

- Configuration sprawl across "stuff in the repo" and "stuff on the VM." Reproducing the setup on a new VM is now a runbook, not a `git pull && docker compose up`.
- nginx upgrades follow the OS package cycle, not your control.
- Frontend static files have to be `rsync`'d to the VM somewhere nginx can read — extra deploy step.

Reasonable if the VM is a long-lived pet. Less reasonable if you ever want to throw away the VM and stand up a fresh one in 5 minutes (which is the point of having the gcloud bootstrap script in [GCPTestInfra.md](GCPTestInfra.md)).

## Recommendation

**Go with A (sibling nginx container in compose).**

Reasons in order:

1. The horizontal-scaling router system you're planning will probably use nginx (or something nginx-shaped). Building familiarity with nginx config now pays off there.
2. Keeps everything in the repo and reproducible via compose. Matches the existing pattern.
3. Container boundary protects you from "I broke nginx, now FastAPI is also down."
4. Easy to migrate to B (prebuilt) later if cert renewal annoys you, or to C (Caddy) if config bloats.

If you want **A but cheaper to bootstrap**, do A with the compose service shape from this doc and a sidecar `certbot/certbot` container called from a host cron once a week. ~10 lines of cron + nginx-reload glue.

If after reading this you actually want the easiest possible thing, do **C** (Caddy). It will work the day you set it up and continue working without your attention. The tradeoff is "less nginx muscle memory."

## Out of scope

- Multi-controller load balancing. Handled by the planned horizontal-scaling router system.
- WAF / DDoS protection. Cloud Armor is overkill for a $50 test infra.
- mTLS between daemons and controller. The current architecture doesn't need it; revisit when device auth lands.
- Auth / RBAC at the application layer. nginx basic auth or IP allowlist on write endpoints is the v1 gate; real auth belongs in FastAPI.

## Concrete next-step checklist (for option A)

1. Pick a domain. Cheapest path: a free subdomain via [DuckDNS](https://www.duckdns.org/) or similar, pointing at the controller VM's external IP. ($0/yr; no registrar account.)
2. Open ports 80 and 443 in the GCP firewall (the [GCPTestInfra.md](GCPTestInfra.md) plan currently only opens 8080 — that becomes private once nginx fronts it).
3. Add `controller/nginx/nginx.conf` and `controller/nginx/Dockerfile` (or just use `nginx:1.27-alpine` with the conf mounted).
4. Modify [docker-compose.yml](../docker-compose.yml): add the `nginx` service; change controller's `ports: ["8080:8080"]` to `expose: ["8080"]`.
5. Get the cert: one-shot `certbot certonly --webroot ...` against the live VM, store in the volume.
6. Update the frontend's `API` constant ([frontend/app.js](../frontend/app.js)) to `''` so it uses same-origin `/clusters`, `/status`, etc. — and update nginx to either pass those paths through directly or rewrite under `/api/`. Pick one and stick with it.
7. Set up cert renewal: weekly host cron running `docker run --rm certbot/certbot renew && docker compose exec nginx nginx -s reload`.
8. Add basic auth or an IP allowlist on `POST /deploy`, `POST /register`, `DELETE /delete`, `POST /deregister`. Read endpoints stay open.

---

## Decision: option C (Caddy), with controller-managed lifecycle

After weighing the alternatives we're going with **Caddy**, but with a twist on option C: instead of declaring the Caddy service in `docker-compose.yml`, the **controller container spawns and reaps Caddy itself** via the Docker API — exactly the pattern the daemon already uses for the QUIC sidecar in [sidecar_supervisor.py](../controller/DeploymentDaemon/sidecar_supervisor.py).

Why this shape:

- One concept ("a thing in front of me that I own") already exists in the codebase. Reusing it keeps the architecture coherent — there are no special compose-level entities that the application doesn't know about.
- The controller's `lifespan` block is the natural place to start and stop dependent processes. Caddy's lifetime tracks the controller's lifetime exactly: if the controller goes down for any reason, the public-facing TLS terminator goes with it (no zombie Caddy serving stale routes to a dead backend).
- Local dev keeps working — set no `CADDY_DOMAIN` and the supervisor is a no-op. Compose comes up identical to before.
- Frontend changes don't require Caddy changes: FastAPI mounts [frontend/](../frontend) via `StaticFiles`, so Caddy is purely a TLS terminator + reverse proxy. Caddyfile stays ~5 lines.

### Architecture

```
                ┌──────────────────────┐
public 443 ───▶ │  caddy:2-alpine      │ (spawned by controller)
                │  TLS + reverse proxy │
                └──────────┬───────────┘
                           │ http://controller:8080
                           ▼
                ┌──────────────────────┐
                │  streambed-controller│
                │  FastAPI + StaticFiles
                │  + caddy_supervisor  │ ──► docker-py ──► Docker socket
                └──────────────────────┘
                           │
                streambed-net (docker bridge)
                           │
                  daemon-edge*, daemon-server*
```

Controller container needs:

- The Docker socket (`/var/run/docker.sock`) mounted, so it can `client.containers.run("caddy:...")`.
- `docker` Python package in `requirements.txt`.
- The `frontend/` directory copied in and mounted at a known path (e.g. `/app/frontend`).
- New env knobs: `CADDY_DOMAIN`, `CADDY_EMAIL`, `CADDY_IMAGE` (default to a baked image like `ashishbasetty/streambed-caddy:latest`).

Caddy container needs:

- To join the `streambed-net` Docker network so it can reach `controller:8080` by service name.
- To bind host ports 80 and 443.
- Caddyfile baked into the image, which references env vars (`{$CADDY_DOMAIN}`, `{$CADDY_EMAIL}`) so one image works across deployments.
- Persistent volume for cert state (`caddy_data` volume) so Let's Encrypt cert survives restarts.

### File layout

```
controller/
  ControllerNode/
    caddy_supervisor.py        # spawn_caddy / kill_caddy
    main.py                    # lifespan: spawn at startup, kill at shutdown
    requirements.txt           # add `docker`
    Dockerfile                 # COPY frontend → /app/frontend
  Caddy/
    Dockerfile                 # FROM caddy:2-alpine, COPY Caddyfile
    Caddyfile                  # 5 lines, env-var-templated
docker-compose.yml             # mount docker.sock, add CADDY_* env, build Caddy image
```

### Caddyfile (final shape)

```caddyfile
{$CADDY_DOMAIN} {
    encode gzip
    reverse_proxy controller:8080
    tls {$CADDY_EMAIL}
}
```

That's it. Caddy auto-issues from Let's Encrypt on first request, renews silently. FastAPI handles both API routes and static frontend, so no `handle /api/*` split is needed.

### Out of scope for this implementation

- IP allowlist / basic auth on write endpoints. Add later as `basic_auth` or `@allowed-write` blocks in the Caddyfile when device auth lands.
- Mutual TLS between daemons and controller. Same — defer to device-auth work.
- Multi-controller load balancing. The horizontal-scaling router system.

### Local-dev fallback

If `CADDY_DOMAIN` is unset, `spawn_caddy` is a no-op (logs and returns). Devs running `docker compose up` keep hitting `http://localhost:8080` directly. No flag needed.

The frontend's `API` constant in [app.js](../frontend/app.js) is now `''` (relative URLs). This works in both modes:

- **Local dev**: open `http://localhost:8080/` — controller serves the frontend, JS calls `/clusters` etc. on the same origin.
- **Prod with Caddy**: open `https://<your domain>/` — Caddy reverse-proxies to the controller, JS calls `/clusters` etc. on the same origin.

### Build & run

```bash
# 1) Build the Caddy image once (it's gated behind a compose profile so it
#    isn't started accidentally — only built):
docker compose --profile build build caddy-build

# 2a) Local dev — Caddy disabled:
docker compose up

# 2b) Production on the GCP VM — set domain & email, then bring up:
export CADDY_DOMAIN=streambed.duckdns.org
export CADDY_EMAIL=you@example.com
docker compose up -d
# The controller will spawn Caddy as soon as its lifespan starts.
# Hit https://<domain>/ to verify TLS.
```

Stopping `docker compose down` reaps Caddy via the controller's lifespan exit. If the controller crashes, the next startup's `spawn_caddy` clears any straggler before recreating. Idempotency is the same as the QUIC sidecar's.

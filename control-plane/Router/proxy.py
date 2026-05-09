"""Proxy logic. Looks up the controller for a request's cluster and forwards."""
import logging

import httpx
from fastapi import HTTPException, Request, Response

from db import lookup_controller, list_unique_controllers

logger = logging.getLogger(__name__)

# Headers we drop when forwarding. host/content-length must be recomputed by httpx.
HOP_BY_HOP = {"host", "content-length", "connection", "transfer-encoding"}

PROXY_TIMEOUT_SECS = 30


async def forward(
    request: Request,
    cluster_name: str,
    client: httpx.AsyncClient,
) -> Response:
    """Forward the incoming request to the controller that owns `cluster_name`."""
    target_base = lookup_controller(cluster_name)
    if not target_base:
        raise HTTPException(404, f"No controller registered for cluster '{cluster_name}'")

    url = f"{target_base.rstrip('/')}{request.url.path}"
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    body = await request.body()

    try:
        upstream = await client.request(
            request.method,
            url,
            params=request.query_params,
            content=body,
            headers=headers,
            timeout=PROXY_TIMEOUT_SECS,
        )
    except httpx.TimeoutException as e:
        logger.warning(f"proxy timeout: cluster={cluster_name} url={url} ({e})")
        raise HTTPException(504, f"Upstream timeout: {target_base}")
    except httpx.HTTPError as e:
        logger.warning(f"proxy connect failed: cluster={cluster_name} url={url} ({e})")
        raise HTTPException(502, f"Upstream unreachable: {target_base}")

    response_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


async def fanout_clusters(client: httpx.AsyncClient) -> list[str]:
    """GET /clusters across every controller in the routing table; merge."""
    controllers = list_unique_controllers()
    seen: set[str] = set()
    for base in controllers:
        try:
            r = await client.get(f"{base.rstrip('/')}/clusters", timeout=PROXY_TIMEOUT_SECS)
            r.raise_for_status()
            for c in r.json().get("clusters", []):
                seen.add(c)
        except httpx.HTTPError as e:
            logger.warning(f"fanout /clusters failed for {base}: {e}")
    return sorted(seen)

"""Docker label constants for StreamBed-owned runtime containers.

IMPORTANT: THESE LABELS ARE METADATA ONLY.

They are NOT the deployment source of truth. The controller's `deployments`
table owns deployment state. These labels exist only so daemon/test cleanup can
identify orphaned Docker resources if controller or daemon state is unavailable.
"""

MANAGED = "streambed.managed"
ROLE = "streambed.role"
CLUSTER = "streambed.cluster"
DEVICE_ID = "streambed.device_id"
OWNER = "streambed.owner"

OWNER_DEPLOYMENT_DAEMON = "deployment-daemon"
ROLE_INFERENCE = "inference"
ROLE_SIDECAR = "sidecar"


def managed_labels(*, cluster: str, device_id: str, role: str) -> dict[str, str]:
    """Return METADATA-ONLY labels for daemon-created Docker resources."""
    return {
        MANAGED: "true",
        ROLE: role,
        CLUSTER: cluster,
        DEVICE_ID: device_id,
        OWNER: OWNER_DEPLOYMENT_DAEMON,
    }


def managed_label_filters(
    *,
    cluster: str | None = None,
    device_id: str | None = None,
    role: str | None = None,
) -> list[str]:
    """Return filters for cleanup/recovery only; not for deployment state."""
    filters = [f"{MANAGED}=true"]
    if cluster is not None:
        filters.append(f"{CLUSTER}={cluster}")
    if device_id is not None:
        filters.append(f"{DEVICE_ID}={device_id}")
    if role is not None:
        filters.append(f"{ROLE}={role}")
    return filters

"""Deployment daemon configuration from environment."""

import os
import platform
from pathlib import Path

DEVICE_ID = os.environ.get("DEVICE_ID", "")
DEVICE_CLUSTER = os.environ.get("DEVICE_CLUSTER", "")
CONTROLLER_URL = (os.environ.get("CONTROLLER_URL") or "").strip()
DEVICE_TYPE = os.environ.get("DEVICE_TYPE")

if not DEVICE_ID:
    raise ValueError("DEVICE_ID is not set")
if not DEVICE_CLUSTER:
    raise ValueError("DEVICE_CLUSTER is not set")
if not CONTROLLER_URL:
    raise ValueError("CONTROLLER_URL is not set")
if not DEVICE_TYPE:
    raise ValueError("DEVICE_TYPE is not set")

DAEMON_PORT = int(os.environ.get("DAEMON_PORT", "9090"))
DAEMON_ADDRESS = os.environ.get("DAEMON_ADDRESS", platform.node())

DEFAULT_HOST_PORT = int(os.environ.get("STREAMBED_HOST_PORT", "8080"))
DEFAULT_CONTAINER_PORT = int(os.environ.get("STREAMBED_CONTAINER_PORT", "80"))
STREAMBED_MEMORY_LIMIT = os.environ.get("STREAMBED_MEMORY_LIMIT", "6g")

INGEST_UDP_PORT = int(os.environ.get("INGEST_UDP_PORT", "9000"))
MAX_VIDEO_FPS = float(os.environ.get("MAX_VIDEO_FPS", "30"))
MAX_FRAME_PAYLOAD_BYTES = int(os.environ.get("MAX_FRAME_PAYLOAD_BYTES", "50_000_000"))

if MAX_VIDEO_FPS <= 0:
    raise ValueError("MAX_VIDEO_FPS must be greater than 0")

_DATA_DIR = Path(__file__).parent / "data"
STATE_PATH = _DATA_DIR / "deployed.json"
STREAM_TARGET_PATH = _DATA_DIR / "stream-target.json"

# Optional - used when deploying edge containers
STREAMBED_CONFIG_HOST_PATH = os.environ.get("STREAMBED_CONFIG_HOST_PATH")
STREAMBED_DATA_HOST_PATH = os.environ.get("STREAMBED_DATA_HOST_PATH")
VIDEO_SOURCE = os.environ.get("VIDEO_SOURCE")

SIDECAR_IMAGE = os.environ.get("SIDECAR_IMAGE", "ashishbasetty/streambed-quic-sidecar:latest")
SIDECAR_LOCAL_UDP_PORT = int(os.environ.get("SIDECAR_LOCAL_UDP_PORT", "9050"))
SIDECAR_QUIC_BIND_PORT = int(os.environ.get("SIDECAR_QUIC_BIND_PORT", "4433"))

# Reverse-path wiring (edge ↔ server, server originates, edge consumes).
# Edge sidecar forwards non-FBCK control msgs to <DEVICE_ID>:SIDECAR_RECV_PORT,
# which is where the local inference container's CSTR listener binds.
# Server sidecar opens a UDP listener on SIDECAR_SERVER_REVERSE_BIND_PORT for
# outbound application data (e.g. advisor advice) to be pumped onto the QUIC
# control stream. Empty / unset on either side disables the corresponding
# half — required for backward compat with the original (forward-only) flow.
SIDECAR_RECV_PORT = int(os.environ.get("SIDECAR_RECV_PORT", "0"))
SIDECAR_SERVER_REVERSE_BIND_PORT = int(os.environ.get("SIDECAR_SERVER_REVERSE_BIND_PORT", "0"))


# Device Registration retry configuration
REGISTER_RETRIES = 5
REGISTER_RETRY_DELAY = 2.0

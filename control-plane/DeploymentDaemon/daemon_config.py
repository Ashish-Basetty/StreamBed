"""Deployment daemon configuration from environment."""

import os
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

# In-container bind port (uvicorn listens here).
DAEMON_PORT = int(os.environ.get("DAEMON_PORT", "9090"))

# Host-routable address the controller uses to reach this daemon.
# Local Docker: `host.docker.internal` (require `extra_hosts: host-gateway` in compose).
# GCP: the VM's internal subnet IP.
DAEMON_PUBLIC_IP = os.environ.get("DAEMON_PUBLIC_IP", "").strip()
if not DAEMON_PUBLIC_IP:
    raise ValueError("DAEMON_PUBLIC_IP is not set")

# Host-published port that maps to DAEMON_PORT inside the container.
# In compose, each daemon publishes a unique host port (e.g. 9090, 9091…).
DAEMON_PUBLIC_PORT = int(os.environ.get("DAEMON_PUBLIC_PORT", "0"))
if DAEMON_PUBLIC_PORT <= 0:
    raise ValueError("DAEMON_PUBLIC_PORT must be set to the host-published port for this daemon")

# Docker network this daemon owns. Daemon-spawned sidecar + inference attach here.
DEVICE_NETWORK_NAME = os.environ.get(
    "DEVICE_NETWORK_NAME", f"streambed-device-{DEVICE_ID}-net"
)

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
VIDEO_SERVER_HOST = os.environ.get("VIDEO_SERVER_HOST", "")
VIDEO_SERVER_PORT = int(os.environ.get("VIDEO_SERVER_PORT", "9200"))

SIDECAR_IMAGE = os.environ.get("SIDECAR_IMAGE", "ashishbasetty/streambed-quic-sidecar:latest")
SIDECAR_LOCAL_UDP_PORT = int(os.environ.get("SIDECAR_LOCAL_UDP_PORT", "9050"))
SIDECAR_QUIC_BIND_PORT = int(os.environ.get("SIDECAR_QUIC_BIND_PORT", "4433"))

# Host UDP port pool for sidecar QUIC publication. Daemon picks a free port
# from this inclusive range before spawning the sidecar.
SIDECAR_PORT_RANGE_MIN = int(os.environ.get("SIDECAR_PORT_RANGE_MIN", "7000"))
SIDECAR_PORT_RANGE_MAX = int(os.environ.get("SIDECAR_PORT_RANGE_MAX", "7999"))
if SIDECAR_PORT_RANGE_MIN <= 0 or SIDECAR_PORT_RANGE_MAX < SIDECAR_PORT_RANGE_MIN:
    raise ValueError("SIDECAR_PORT_RANGE_MIN/MAX invalid")

# Device Registration retry configuration
REGISTER_RETRIES = 5
REGISTER_RETRY_DELAY = 2.0

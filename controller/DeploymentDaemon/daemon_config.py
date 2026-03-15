"""Deployment daemon configuration from environment."""

import os
import platform
from pathlib import Path

DEVICE_ID = os.environ.get("DEVICE_ID", "")
DEVICE_CLUSTER = os.environ.get("DEVICE_CLUSTER", "")
CONTROLLER_URL = (os.environ.get("CONTROLLER_URL") or "").strip()

if not DEVICE_ID:
    raise ValueError("DEVICE_ID is not set")
if not DEVICE_CLUSTER:
    raise ValueError("DEVICE_CLUSTER is not set")
if not CONTROLLER_URL:
    raise ValueError("CONTROLLER_URL is not set")

DAEMON_PORT = int(os.environ.get("DAEMON_PORT", "9090"))
DAEMON_ADDRESS = os.environ.get("DAEMON_ADDRESS", platform.node())

DEFAULT_HOST_PORT = int(os.environ.get("STREAMBED_HOST_PORT", "8080"))
DEFAULT_CONTAINER_PORT = int(os.environ.get("STREAMBED_CONTAINER_PORT", "80"))
STREAMBED_MEMORY_LIMIT = os.environ.get("STREAMBED_MEMORY_LIMIT", "6g")

STREAM_PROXY_PORT = int(os.environ.get("STREAM_PROXY_PORT", "9000"))
STREAM_TARGET_POLL_INTERVAL = float(os.environ.get("STREAM_TARGET_POLL_INTERVAL", "2.0"))
BANDWIDTH_POLL_INTERVAL = float(os.environ.get("BANDWIDTH_POLL_INTERVAL", "1.0"))
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
STREAM_PROXY_HOST = os.environ.get("STREAM_PROXY_HOST") or DAEMON_ADDRESS


# Device Registration retry configuration
REGISTER_RETRIES = 5
REGISTER_RETRY_DELAY = 2.0

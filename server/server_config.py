import os

DEVICE_ID = os.getenv("DEVICE_ID", "server-001")
DEVICE_CLUSTER = os.getenv("DEVICE_CLUSTER", "default")
CONTROLLER_URL = os.getenv("CONTROLLER_URL", "")  # e.g. http://controller:8080
STORAGE_DIR = os.getenv("STORAGE_DIR", "/data/streambed")
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8001"))
MODEL_DEVICE = os.getenv("MODEL_DEVICE", "cpu")  # "cuda" if GPU available
STREAM_LISTEN_HOST = os.getenv("STREAM_LISTEN_HOST", "0.0.0.0")
STREAM_LISTEN_PORT = int(os.getenv("STREAM_LISTEN_PORT", "9000"))
TTL_MAX = float(os.getenv("TTL_MAX", "7200"))  # Server has more storage
TTL_MIN = float(os.getenv("TTL_MIN", "60"))
CLEANUP_INTERVAL = float(os.getenv("CLEANUP_INTERVAL", "120"))

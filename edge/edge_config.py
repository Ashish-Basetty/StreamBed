import os

DEVICE_ID = os.getenv("DEVICE_ID", "edge-001")
DEVICE_CLUSTER = os.getenv("DEVICE_CLUSTER", "default")
CONTROLLER_URL = os.getenv("CONTROLLER_URL", "")  # e.g. http://controller:8080
STORAGE_DIR = os.getenv("STORAGE_DIR", "/data/streambed")
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
MODEL_DEVICE = os.getenv("MODEL_DEVICE", "cpu")
VIDEO_SOURCE = os.getenv("VIDEO_SOURCE", "0")  # "0" for webcam, or path to file
SERVER_HOST = os.getenv("SERVER_HOST", "server")
SERVER_PORT = int(os.getenv("SERVER_PORT", "9000"))
TTL_MAX = float(os.getenv("TTL_MAX", "3600"))
TTL_MIN = float(os.getenv("TTL_MIN", "30"))
CLEANUP_INTERVAL = float(os.getenv("CLEANUP_INTERVAL", "60"))

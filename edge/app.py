import asyncio
import struct
import sys
import time
import uuid
from contextlib import asynccontextmanager

import numpy as np
import uvicorn
from fastapi import FastAPI

sys.path.insert(0, "/app")

from edge_config import (
    API_HOST,
    API_PORT,
    CLEANUP_INTERVAL,
    DEVICE_ID,
    MODEL_DEVICE,
    SIDECAR_HOST,
    SIDECAR_UDP_PORT,
    STORAGE_DIR,
    TTL_MAX,
    TTL_MIN,
    VIDEO_SERVER_HOST,
    VIDEO_SERVER_PORT,
)
from shared.api.retrieval import create_retrieval_router
from shared.inference.mobilenet import MobileNetV2Model
from shared.interfaces.stream_interface import StreamBedUDPSender, StreamFrame
from shared.storage.frame_store import FrameStore
from shared.storage.ttl_manager import TTLManager
from shared.tcp_framing import CHUNK_MAGIC, read_message

model = MobileNetV2Model(device=MODEL_DEVICE)
store = FrameStore(base_dir=STORAGE_DIR)
ttl_mgr = TTLManager(storage_path=STORAGE_DIR, max_ttl=TTL_MAX, min_ttl=TTL_MIN)
sender = StreamBedUDPSender()

_SHAPE_FMT = ">HHH"
_SHAPE_SIZE = struct.calcsize(_SHAPE_FMT)
_VIDEO_CONNECT_RETRY = 5


async def video_capture_loop():
    """Connect to video server and process received frames."""
    while True:
        if not (VIDEO_SERVER_HOST and VIDEO_SERVER_HOST.strip()):
            print("[Edge] VIDEO_SERVER_HOST not set, waiting...")
            await asyncio.sleep(_VIDEO_CONNECT_RETRY)
            continue
        try:
            reader, writer = await asyncio.open_connection(VIDEO_SERVER_HOST, VIDEO_SERVER_PORT)
            print(f"[Edge] Connected to video server {VIDEO_SERVER_HOST}:{VIDEO_SERVER_PORT}")
            try:
                while True:
                    tag, payload = await read_message(reader)
                    if tag != CHUNK_MAGIC:
                        continue
                    h, w, c = struct.unpack(_SHAPE_FMT, payload[:_SHAPE_SIZE])
                    frame = np.frombuffer(payload[_SHAPE_SIZE:], dtype=np.uint8).reshape(h, w, c)

                    timestamp = time.time()
                    frame_id = f"{DEVICE_ID}_{uuid.uuid4().hex[:12]}"

                    result = model.process_frame(frame)
                    ttl = ttl_mgr.compute_ttl()
                    store.store(
                        frame_id, timestamp, frame, result.embedding,
                        model.get_model_version(), ttl,
                    )

                    sf = StreamFrame(
                        timestamp=timestamp,
                        frame=frame,
                        embedding=result.embedding,
                        model_version=model.get_model_version(),
                        source_device_id=DEVICE_ID,
                        frame_interleaving_rate=30.0,
                    )
                    sent = await sender.send(sf)

                    frame_count = store.count()
                    if frame_count % 10 == 1 or frame_count <= 5:
                        print(f"[Edge] Frame {frame_count} | {frame_id} | "
                              f"label={result.label} conf={result.confidence:.3f} | "
                              f"ttl={ttl:.0f}s | sent={sent}")
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        except (ConnectionRefusedError, OSError) as e:
            print(f"[Edge] Video server {VIDEO_SERVER_HOST}:{VIDEO_SERVER_PORT} unreachable: {e}, "
                  f"retrying in {_VIDEO_CONNECT_RETRY}s...")
            await asyncio.sleep(_VIDEO_CONNECT_RETRY)


async def ttl_cleanup_loop():
    """Periodically delete expired frames/embeddings."""
    while True:
        deleted = store.delete_expired()
        if deleted > 0:
            print(f"[Edge] TTL cleanup: removed {deleted} expired entries")
        await asyncio.sleep(CLEANUP_INTERVAL)


CONNECT_RETRY_INTERVAL = 5
async def _connect_to_proxy_with_retry() -> None:
    """Retry connecting to the sidecar until success. Spins if host is unset or unreachable."""
    while True:
        if not (SIDECAR_HOST and SIDECAR_HOST.strip()):
            print("[Edge] SIDECAR_HOST not set, waiting...")
            await asyncio.sleep(CONNECT_RETRY_INTERVAL)
            continue
        try:
            await sender.connect(SIDECAR_HOST, SIDECAR_UDP_PORT)
            return
        except Exception as e:
            print(f"[Edge] Cannot connect to {SIDECAR_HOST}:{SIDECAR_UDP_PORT}: {e}, retrying in {CONNECT_RETRY_INTERVAL}s...")
            await asyncio.sleep(CONNECT_RETRY_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Edge] Loading model...")
    model.load()
    print("[Edge] Model loaded.")

    cleanup_task = asyncio.create_task(ttl_cleanup_loop())

    await _connect_to_proxy_with_retry()
    capture_task = asyncio.create_task(video_capture_loop())

    yield

    capture_task.cancel()
    cleanup_task.cancel()
    await sender.close()


app = FastAPI(title="StreamBed Edge", lifespan=lifespan)
app.include_router(create_retrieval_router(store))

if __name__ == "__main__":
    uvicorn.run(app, host=API_HOST, port=API_PORT)

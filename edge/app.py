import asyncio
import sys
import time
import uuid

import cv2
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI

sys.path.insert(0, "/app")

from shared.inference.mobilenet import MobileNetV2Model
from shared.storage.frame_store import FrameStore
from shared.storage.ttl_manager import TTLManager
from shared.api.retrieval import create_retrieval_router
from shared.interfaces.controller_interface import MockController
from shared.interfaces.stream_interface import MockStreamSender, StreamFrame
from edge_config import (
    API_HOST,
    API_PORT,
    CLEANUP_INTERVAL,
    DEVICE_ID,
    MODEL_DEVICE,
    SERVER_HOST,
    SERVER_PORT,
    STORAGE_DIR,
    TTL_MAX,
    TTL_MIN,
    VIDEO_SOURCE,
)

model = MobileNetV2Model(device=MODEL_DEVICE)
store = FrameStore(base_dir=STORAGE_DIR)
ttl_mgr = TTLManager(storage_path=STORAGE_DIR, max_ttl=TTL_MAX, min_ttl=TTL_MIN)
controller = MockController()
sender = MockStreamSender()


async def video_capture_loop():
    """Continuously capture frames, run inference, store, and stream."""
    source = int(VIDEO_SOURCE) if VIDEO_SOURCE.isdigit() else VIDEO_SOURCE
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"[Edge] Cannot open video source: {VIDEO_SOURCE}")
        return

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                # End of video file — loop back to start
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                await asyncio.sleep(0.01)
                continue

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
            )
            await sender.send(sf)

            # ~30 fps cap
            await asyncio.sleep(0.033)
    finally:
        cap.release()


async def ttl_cleanup_loop():
    """Periodically delete expired frames/embeddings."""
    while True:
        deleted = store.delete_expired()
        if deleted > 0:
            print(f"[Edge] TTL cleanup: removed {deleted} expired entries")
        await asyncio.sleep(CLEANUP_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Edge] Loading model...")
    model.load()
    print("[Edge] Model loaded.")

    await controller.register(DEVICE_ID, "edge", model.get_model_version())
    await sender.connect(SERVER_HOST, SERVER_PORT)

    capture_task = asyncio.create_task(video_capture_loop())
    cleanup_task = asyncio.create_task(ttl_cleanup_loop())

    yield

    capture_task.cancel()
    cleanup_task.cancel()
    await sender.close()


app = FastAPI(title="StreamBed Edge", lifespan=lifespan)
app.include_router(create_retrieval_router(store))

if __name__ == "__main__":
    uvicorn.run(app, host=API_HOST, port=API_PORT)

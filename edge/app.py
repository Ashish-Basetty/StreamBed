import asyncio
import sys
import time
import uuid

import cv2
import httpx
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI

sys.path.insert(0, "/app")

from shared.inference.mobilenet import MobileNetV2Model
from shared.storage.frame_store import FrameStore
from shared.storage.ttl_manager import TTLManager
from shared.api.retrieval import create_retrieval_router
from shared.interfaces.stream_interface import StreamBedUDPSender, StreamFrame
from edge_config import (
    API_HOST,
    API_PORT,
    CLEANUP_INTERVAL,
    CONTROLLER_URL,
    DEVICE_CLUSTER,
    DEVICE_ID,
    MODEL_DEVICE,
    SERVER_HOST,
    SERVER_PORT,
    STORAGE_DIR,
    TTL_MAX,
    TTL_MIN,
    VIDEO_SOURCE,
)

POLL_INTERVAL = 30

model = MobileNetV2Model(device=MODEL_DEVICE)
store = FrameStore(base_dir=STORAGE_DIR)
ttl_mgr = TTLManager(storage_path=STORAGE_DIR, max_ttl=TTL_MAX, min_ttl=TTL_MIN)
sender = StreamBedUDPSender()


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
                frame_interleaving_rate=30.0,
            )
            sent = await sender.send(sf)

            frame_count = store.count()
            if frame_count % 10 == 1 or frame_count <= 5:
                print(f"[Edge] Frame {frame_count} | {frame_id} | "
                      f"label={result.label} conf={result.confidence:.3f} | "
                      f"ttl={ttl:.0f}s | sent={sent}")

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


async def heartbeat_loop():
    """Send status heartbeats to the controller."""
    while True:
        if CONTROLLER_URL and CONTROLLER_URL.strip():
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(
                        f"{CONTROLLER_URL.rstrip('/')}/heartbeat",
                        json={
                            "device_cluster": DEVICE_CLUSTER,
                            "device_id": DEVICE_ID,
                            "current_model_version": model.get_model_version(),
                            "status": "Active",
                        },
                    )
            except Exception as e:
                print(f"[Edge] heartbeat failed: {e}")
        await asyncio.sleep(30)


async def config_poll_loop():
    """Periodically ask the controller what we should be running."""
    while True:
        if CONTROLLER_URL and CONTROLLER_URL.strip():
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(
                        f"{CONTROLLER_URL.rstrip('/')}/config",
                        params={
                            "device_cluster": DEVICE_CLUSTER,
                            "device_id": DEVICE_ID,
                        },
                    )
                    if resp.status_code == 200:
                        cfg = resp.json()
                        # example fields – adjust to whatever your controller returns
                        model_img = cfg.get("model_image")
                        target = cfg.get("stream_target") or {}
                        ip = target.get("ip")
                        port = target.get("port")

                        # 1. if the controller has asked for a new container/model,
                        #    trigger the deployment daemon (or directly pull & restart).
                        # 2. if the stream target changed, update sender/receiver.
                        #    (the sender/receiver already have a `connect`/`listen`
                        #    method you can call again here).
                        #
                        # The code below is a stub; replace with real logic.
                        if ip and port:
                            await sender.connect(ip, port)   # edge only
            except Exception as e:
                print(f"[Edge] config poll failed: {e}")
        await asyncio.sleep(POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Edge] Loading model...")
    model.load()
    print("[Edge] Model loaded.")

    await sender.connect(SERVER_HOST, SERVER_PORT)

    capture_task = asyncio.create_task(video_capture_loop())
    cleanup_task = asyncio.create_task(ttl_cleanup_loop())
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    config_task  = asyncio.create_task(config_poll_loop())

    yield

    capture_task.cancel()
    cleanup_task.cancel()
    heartbeat_task.cancel()
    config_task.cancel()
    await sender.close()


app = FastAPI(title="StreamBed Edge", lifespan=lifespan)
app.include_router(create_retrieval_router(store))

if __name__ == "__main__":
    uvicorn.run(app, host=API_HOST, port=API_PORT)

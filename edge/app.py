import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

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


async def stream_target_poll_loop():
    """Poll the stream target config file from the daemon and reconnect sender if needed."""
    stream_target_path = Path("/config/stream-target.json")
    last_target = None
    
    while True:
        try:
            if stream_target_path.exists():
                data = json.loads(stream_target_path.read_text())
                target_ip = data.get("target_ip")
                target_port = data.get("target_port")
                
                current_target = (target_ip, target_port) if target_ip and target_port else None
                
                if current_target and current_target != last_target:
                    print(f"[Edge] Stream target changed to {target_ip}:{target_port}, reconnecting...")
                    await sender.connect(target_ip, target_port)
                    last_target = current_target
        except Exception as e:
            print(f"[Edge] stream_target_poll_loop error: {e}")
        
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
    stream_target_task = asyncio.create_task(stream_target_poll_loop())

    yield

    capture_task.cancel()
    cleanup_task.cancel()
    heartbeat_task.cancel()
    stream_target_task.cancel()
    await sender.close()


app = FastAPI(title="StreamBed Edge", lifespan=lifespan)
app.include_router(create_retrieval_router(store))

if __name__ == "__main__":
    uvicorn.run(app, host=API_HOST, port=API_PORT)

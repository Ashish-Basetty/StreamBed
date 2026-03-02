import asyncio
import sys
import uuid

import httpx
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI

sys.path.insert(0, "/app")

from shared.inference.mobilenet import MobileNetV2Model
from shared.storage.frame_store import FrameStore
from shared.storage.ttl_manager import TTLManager
from shared.api.retrieval import create_retrieval_router
from shared.interfaces.stream_interface import MockStreamReceiver
from server_config import (
    API_HOST,
    API_PORT,
    CLEANUP_INTERVAL,
    CONTROLLER_URL,
    DEVICE_CLUSTER,
    DEVICE_ID,
    MODEL_DEVICE,
    STORAGE_DIR,
    STREAM_LISTEN_HOST,
    STREAM_LISTEN_PORT,
    TTL_MAX,
    TTL_MIN,
)

model = MobileNetV2Model(device=MODEL_DEVICE)
store = FrameStore(base_dir=STORAGE_DIR)
ttl_mgr = TTLManager(storage_path=STORAGE_DIR, max_ttl=TTL_MAX, min_ttl=TTL_MIN)
receiver = MockStreamReceiver()


async def stream_receive_loop():
    """Receive frames from edge devices and run server-side inference."""
    await receiver.listen(STREAM_LISTEN_HOST, STREAM_LISTEN_PORT)
    async for stream_frame in receiver.receive_stream():
        if stream_frame.frame is not None:
            result = model.process_frame(stream_frame.frame)
            frame_id = f"{DEVICE_ID}_{uuid.uuid4().hex[:12]}"
            ttl = ttl_mgr.compute_ttl()
            store.store(
                frame_id, stream_frame.timestamp, stream_frame.frame,
                result.embedding, model.get_model_version(), ttl,
            )


async def ttl_cleanup_loop():
    """Periodically delete expired frames/embeddings."""
    while True:
        deleted = store.delete_expired()
        if deleted > 0:
            print(f"[Server] TTL cleanup: removed {deleted} expired entries")
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
                print(f"[Server] heartbeat failed: {e}")
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Server] Loading model...")
    model.load()
    print("[Server] Model loaded.")

    if CONTROLLER_URL and CONTROLLER_URL.strip():
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{CONTROLLER_URL.rstrip('/')}/register",
                json={
                    "device_cluster": DEVICE_CLUSTER,
                    "device_id": DEVICE_ID,
                    "device_type": "server",
                    "current_model_version": model.get_model_version(),
                },
            )

    receive_task = asyncio.create_task(stream_receive_loop())
    cleanup_task = asyncio.create_task(ttl_cleanup_loop())
    heartbeat_task = asyncio.create_task(heartbeat_loop())

    yield

    receive_task.cancel()
    cleanup_task.cancel()
    heartbeat_task.cancel()
    await receiver.stop()


app = FastAPI(title="StreamBed Server", lifespan=lifespan)
app.include_router(create_retrieval_router(store))

if __name__ == "__main__":
    uvicorn.run(app, host=API_HOST, port=API_PORT)

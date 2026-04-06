import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI

sys.path.insert(0, "/app")

from shared.inference.mobilenet import MobileNetV2Model
from shared.storage.frame_store import FrameStore
from shared.storage.ttl_manager import TTLManager
from shared.api.retrieval import create_retrieval_router
from shared.interfaces.stream_interface import StreamBedUDPServerReceiver
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

receiver = StreamBedUDPServerReceiver()


async def stream_receive_loop():
    await receiver.listen(STREAM_LISTEN_HOST, STREAM_LISTEN_PORT)
    frame_count = 0
    async for stream_frame in receiver.receive_stream():
        frame_count += 1
        src = stream_frame.source_device_id
        has_frame = stream_frame.frame is not None
        has_emb = stream_frame.embedding is not None
        print(f"[Server] Received #{frame_count} from {src} "
              f"(frame={has_frame}, embedding={has_emb}, "
              f"ts={stream_frame.timestamp:.3f})")
        frame_id = f"{DEVICE_ID}_{uuid.uuid4().hex[:12]}"
        ttl = ttl_mgr.compute_ttl()
        if stream_frame.frame is not None:
            result = model.process_frame(stream_frame.frame)
            store.store(
                frame_id, stream_frame.timestamp, stream_frame.frame,
                result.embedding, model.get_model_version(), ttl,
            )
            print(f"[Server] Stored {frame_id} | label={result.label} "
                  f"conf={result.confidence:.3f} | "
                  f"stored_total={store.count()}")
        elif stream_frame.embedding is not None:
            store.store(
                frame_id, stream_frame.timestamp, None,
                stream_frame.embedding, stream_frame.model_version, ttl,
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


async def stream_target_poll_loop():
    """Poll the stream target config file from the daemon for informational purposes."""
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
                    print(f"[Server] Stream target config changed to {target_ip}:{target_port}")
                    last_target = current_target
        except Exception as e:
            print(f"[Server] stream_target_poll_loop error: {e}")
        
        await asyncio.sleep(30)


async def _feedback_send_loop():
    """Push received_bps to stream source via UDP every 2 seconds."""
    while True:
        await asyncio.sleep(2.0)
        addr = receiver.stream_source_addr
        if addr is None:
            continue
        now = time.monotonic()
        cutoff = now - 2.0
        total_bits = sum(n * 8 for t, n in receiver.stream_received if t >= cutoff)
        received_bps = total_bits / 2.0
        payload = json.dumps({"received_bps": received_bps}).encode("utf-8")
        try:
            receiver.send_datagram(payload, addr)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Server] Loading model...")
    model.load()
    print("[Server] Model loaded.")

    receive_task = asyncio.create_task(stream_receive_loop())
    cleanup_task = asyncio.create_task(ttl_cleanup_loop())
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    stream_target_task = asyncio.create_task(stream_target_poll_loop())
    feedback_task = asyncio.create_task(_feedback_send_loop())

    yield

    receive_task.cancel()
    feedback_task.cancel()
    cleanup_task.cancel()
    heartbeat_task.cancel()
    stream_target_task.cancel()
    try:
        await feedback_task
    except asyncio.CancelledError:
        pass
    await receiver.stop()


app = FastAPI(title="StreamBed Server", lifespan=lifespan)
app.include_router(create_retrieval_router(store))
if __name__ == "__main__":
    uvicorn.run(app, host=API_HOST, port=API_PORT)

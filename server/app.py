import asyncio
import json
import sys
from pathlib import Path

import httpx
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI

sys.path.insert(0, "/app")

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
    HEARTBEAT_INTERVAL,
    STORAGE_DIR,
    STREAM_LISTEN_HOST,
    STREAM_LISTEN_PORT,
    TTL_MAX,
    TTL_MIN,
)

# Server is now a passive sink: no inference, just stores whatever halves
# arrive (frame from CHNK, embedding from EMBD) and merges them by
# (source_device_id, timestamp). The edge runs the model; the server records.
# When inference comes back to the server later, it reads from this same
# store rather than running on the receive path.
PASSTHROUGH_MODEL_VERSION = "passthrough"

store = FrameStore(base_dir=STORAGE_DIR)
ttl_mgr = TTLManager(storage_path=STORAGE_DIR, max_ttl=TTL_MAX, min_ttl=TTL_MIN)

receiver = StreamBedUDPServerReceiver()


def _frame_id(source_device_id: str, timestamp: float) -> str:
    """Stable key per logical edge frame, so frame-only and embedding-only
    halves merge into the same row."""
    return f"{source_device_id}_{timestamp:.6f}"


def store_from_stream_frame(target_store: FrameStore, stream_frame, ttl_seconds: float) -> None:
    """Persist one half of a logical frame into `target_store`.

    Free function (not a method) so tests can drive the server's exact
    dispatch logic without standing up the FastAPI app.
    """
    target_store.store_partial(
        frame_id=_frame_id(stream_frame.source_device_id, stream_frame.timestamp),
        timestamp=stream_frame.timestamp,
        frame=stream_frame.frame,
        embedding=stream_frame.embedding,
        model_version=stream_frame.model_version or None,
        ttl_seconds=ttl_seconds,
    )


async def stream_receive_loop():
    await receiver.listen(STREAM_LISTEN_HOST, STREAM_LISTEN_PORT)
    frame_count = 0
    async for stream_frame in receiver.receive_stream():
        frame_count += 1
        src = stream_frame.source_device_id
        has_frame = stream_frame.frame is not None
        has_emb = stream_frame.embedding is not None
        print(
            f"[Server] Received #{frame_count} from {src} "
            f"(frame={has_frame}, embedding={has_emb}, "
            f"ts={stream_frame.timestamp:.3f})"
        )
        store_from_stream_frame(store, stream_frame, ttl_mgr.compute_ttl())


async def ttl_cleanup_loop():
    """Periodically delete expired frames/embeddings."""
    while True:
        deleted = store.delete_expired()
        if deleted > 0:
            print(f"[Server] TTL cleanup: removed {deleted} expired entries")
        await asyncio.sleep(CLEANUP_INTERVAL)


async def heartbeat_loop():
    """Send status heartbeats to the controller. No model runs here, so
    `current_model_version` is the passthrough sentinel; the actual edge
    model_version is stored per-row instead."""
    while True:
        if CONTROLLER_URL and CONTROLLER_URL.strip():
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(
                        f"{CONTROLLER_URL.rstrip('/')}/heartbeat",
                        json={
                            "device_cluster": DEVICE_CLUSTER,
                            "device_id": DEVICE_ID,
                            "current_model_version": PASSTHROUGH_MODEL_VERSION,
                            "status": "Active",
                        },
                    )
            except Exception as e:
                print(f"[Server] heartbeat failed: {e}")
        await asyncio.sleep(HEARTBEAT_INTERVAL)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Server] Starting in passthrough mode (no inference).")

    receive_task = asyncio.create_task(stream_receive_loop())
    cleanup_task = asyncio.create_task(ttl_cleanup_loop())
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    stream_target_task = asyncio.create_task(stream_target_poll_loop())

    yield

    receive_task.cancel()
    cleanup_task.cancel()
    heartbeat_task.cancel()
    stream_target_task.cancel()
    await receiver.stop()


app = FastAPI(title="StreamBed Server", lifespan=lifespan)
app.include_router(create_retrieval_router(store))
if __name__ == "__main__":
    uvicorn.run(app, host=API_HOST, port=API_PORT)

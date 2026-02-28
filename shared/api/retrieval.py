import base64
from typing import Optional

import numpy as np
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from shared.storage.frame_store import FrameStore


class FrameResponse(BaseModel):
    frame_id: str
    timestamp: float
    model_version: str
    frame_jpeg_b64: str
    embedding: Optional[list[float]]


class QueryResponse(BaseModel):
    count: int
    frames: list[FrameResponse]


class HealthResponse(BaseModel):
    status: str
    stored_frames: int


def create_retrieval_router(frame_store: FrameStore) -> APIRouter:
    """Factory: creates a router bound to the given FrameStore instance."""
    router = APIRouter(prefix="/api/v1", tags=["retrieval"])

    @router.get("/health", response_model=HealthResponse)
    async def health():
        return HealthResponse(status="ok", stored_frames=frame_store.count())

    @router.get("/frames", response_model=QueryResponse)
    async def get_frames(
        start: float = Query(..., description="Start unix timestamp"),
        end: float = Query(..., description="End unix timestamp"),
    ):
        """Retrieve frames + embeddings by timestamp range."""
        if start > end:
            raise HTTPException(400, "start must be <= end")
        results = frame_store.query_by_timestamp(start, end)
        frames = []
        for sf in results:
            with open(sf.frame_path, "rb") as f:
                jpeg_b64 = base64.b64encode(f.read()).decode("ascii")
            emb = None
            if sf.embedding_path:
                emb = np.load(sf.embedding_path).tolist()
            frames.append(
                FrameResponse(
                    frame_id=sf.frame_id,
                    timestamp=sf.timestamp,
                    model_version=sf.model_version,
                    frame_jpeg_b64=jpeg_b64,
                    embedding=emb,
                )
            )
        return QueryResponse(count=len(frames), frames=frames)

    @router.get("/embeddings", response_model=QueryResponse)
    async def get_embeddings(
        start: float = Query(..., description="Start unix timestamp"),
        end: float = Query(..., description="End unix timestamp"),
    ):
        """Retrieve embeddings only (no JPEG data). Lighter payload."""
        if start > end:
            raise HTTPException(400, "start must be <= end")
        results = frame_store.query_by_timestamp(start, end)
        frames = []
        for sf in results:
            emb = None
            if sf.embedding_path:
                emb = np.load(sf.embedding_path).tolist()
            frames.append(
                FrameResponse(
                    frame_id=sf.frame_id,
                    timestamp=sf.timestamp,
                    model_version=sf.model_version,
                    frame_jpeg_b64="",
                    embedding=emb,
                )
            )
        return QueryResponse(count=len(frames), frames=frames)

    return router

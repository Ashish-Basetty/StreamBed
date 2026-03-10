import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np


@dataclass
class StoredFrame:
    frame_id: str
    timestamp: float
    frame_path: str
    embedding_path: Optional[str]
    model_version: str
    ttl_expires: float


class FrameStore:
    """Filesystem + SQLite storage for video frames and embeddings."""

    def __init__(self, base_dir: str, db_path: Optional[str] = None):
        self._base_dir = Path(base_dir)
        self._frames_dir = self._base_dir / "frames"
        self._embeddings_dir = self._base_dir / "embeddings"
        self._frames_dir.mkdir(parents=True, exist_ok=True)
        self._embeddings_dir.mkdir(parents=True, exist_ok=True)

        db = db_path or str(self._base_dir / "metadata.db")
        self._conn = sqlite3.connect(db, check_same_thread=False)
        self._init_db()

    def _init_db(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS frames (
                frame_id TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                frame_path TEXT NOT NULL,
                embedding_path TEXT,
                model_version TEXT NOT NULL,
                ttl_expires REAL NOT NULL
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_timestamp ON frames(timestamp)"
        )
        self._conn.commit()

    def store(
        self,
        frame_id: str,
        timestamp: float,
        frame: Optional[np.ndarray],
        embedding: Optional[np.ndarray],
        model_version: str,
        ttl_seconds: float,
    ) -> StoredFrame:
        frame_path = ""
        if frame is not None:
            frame_path = str(self._frames_dir / f"{frame_id}.jpg")
            cv2.imwrite(frame_path, frame)

        emb_path = None
        if embedding is not None:
            emb_path = str(self._embeddings_dir / f"{frame_id}.npy")
            np.save(emb_path, embedding)

        expires = time.time() + ttl_seconds
        self._conn.execute(
            "INSERT OR REPLACE INTO frames VALUES (?, ?, ?, ?, ?, ?)",
            (frame_id, timestamp, frame_path, emb_path, model_version, expires),
        )
        self._conn.commit()
        return StoredFrame(
            frame_id, timestamp, frame_path, emb_path, model_version, expires
        )

    def query_by_timestamp(self, start: float, end: float) -> list[StoredFrame]:
        """Retrieve all frames/embeddings within [start, end] timestamp range."""
        rows = self._conn.execute(
            "SELECT * FROM frames WHERE timestamp >= ? AND timestamp <= ? "
            "ORDER BY timestamp ASC",
            (start, end),
        ).fetchall()
        return [StoredFrame(*r) for r in rows]

    def delete_expired(self) -> int:
        """Remove expired entries from disk and database. Returns count deleted."""
        now = time.time()
        rows = self._conn.execute(
            "SELECT frame_id, frame_path, embedding_path FROM frames "
            "WHERE ttl_expires <= ?",
            (now,),
        ).fetchall()
        for _, fp, ep in rows:
            if os.path.exists(fp):
                os.remove(fp)
            if ep and os.path.exists(ep):
                os.remove(ep)
        self._conn.execute("DELETE FROM frames WHERE ttl_expires <= ?", (now,))
        self._conn.commit()
        return len(rows)

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]

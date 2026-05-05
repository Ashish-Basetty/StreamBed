"""SQLite-backed cluster_routing table.

One-writer / many-reader semantics enforced by:
  - WAL journal mode (concurrent reads, single writer doesn't block readers)
  - All writes funneled through admin.py handlers; everything else is read-only
"""
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("ROUTER_DB_PATH", "/app/data/router.db"))


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cluster_routing (
                cluster_name   TEXT PRIMARY KEY,
                controller_url TEXT NOT NULL,
                updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def lookup_controller(cluster_name: str) -> str | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT controller_url FROM cluster_routing WHERE cluster_name=?",
            (cluster_name,),
        ).fetchone()
        return row["controller_url"] if row else None
    finally:
        conn.close()


def list_routes() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT cluster_name, controller_url, updated_at FROM cluster_routing"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_route(cluster_name: str, controller_url: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO cluster_routing (cluster_name, controller_url, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(cluster_name) DO UPDATE SET
                controller_url=excluded.controller_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (cluster_name, controller_url),
        )
        conn.commit()
    finally:
        conn.close()


def delete_route(cluster_name: str) -> bool:
    """Returns True if a row was deleted."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM cluster_routing WHERE cluster_name=?",
            (cluster_name,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_unique_controllers() -> list[str]:
    """Distinct controller_urls across all clusters; used for fan-out."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT controller_url FROM cluster_routing"
        ).fetchall()
        return [r["controller_url"] for r in rows]
    finally:
        conn.close()


def seed_if_empty(default_cluster: str, default_controller_url: str) -> bool:
    """Insert a single default route iff the table is empty.
    Mirrors the controller's lifespan-time `routing` backfill. Returns True if
    a row was inserted, False if the table already had data.
    """
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM cluster_routing").fetchone()["n"]
        if count > 0:
            return False
        conn.execute(
            """INSERT INTO cluster_routing (cluster_name, controller_url, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)""",
            (default_cluster, default_controller_url),
        )
        conn.commit()
        return True
    finally:
        conn.close()

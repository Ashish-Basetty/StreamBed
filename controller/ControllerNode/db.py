"""SQLite database setup and access for the controller node."""
import sqlite3
from pathlib import Path

from heartbeat_spec import HeartbeatStatus

DB_PATH = Path(__file__).parent / "data" / "controller.db"


def get_connection() -> sqlite3.Connection:
    """Get a connection to the database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize the database schema."""
    conn = get_connection()
    try:
        conn.executescript("""
            -- Device registry: device_cluster/device_id -> IP
            CREATE TABLE IF NOT EXISTS devices (
                device_cluster TEXT NOT NULL,
                device_id TEXT NOT NULL,
                ip TEXT NOT NULL,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (device_cluster, device_id)
            );

            -- Status table: heartbeats keyed by device_cluster/device_id
            CREATE TABLE IF NOT EXISTS device_status (
                device_cluster TEXT NOT NULL,
                device_id TEXT NOT NULL,
                current_model TEXT,
                status TEXT,
                last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (device_cluster, device_id)
            );

            -- Routing table: reroute source -> target
            CREATE TABLE IF NOT EXISTS routing (
                source_cluster TEXT NOT NULL,
                source_device TEXT NOT NULL,
                target_cluster TEXT NOT NULL,
                target_device TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source_cluster, source_device)
            );

            CREATE INDEX IF NOT EXISTS idx_devices_cluster ON devices(device_cluster);
            CREATE INDEX IF NOT EXISTS idx_status_heartbeat ON device_status(last_heartbeat);
        """)
        # Migration: add port column if missing (daemon listen port, default 9090)
        try:
            conn.execute("ALTER TABLE devices ADD COLUMN port INTEGER")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
    finally:
        conn.close()


def register_device(
    device_cluster: str,
    device_id: str,
    ip: str,
    port: int | None = None,
) -> None:
    """Register or update a device in the registry."""
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO devices (device_cluster, device_id, ip, port, registered_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(device_cluster, device_id) DO UPDATE SET
                ip = excluded.ip,
                port = excluded.port,
                registered_at = CURRENT_TIMESTAMP
            """,
            (device_cluster, device_id, ip, port),
        )
        conn.commit()
    finally:
        conn.close()

def deregister_device(
    device_cluster: str,
    device_id: str,
) -> None:
    """Deregister a device from the registry."""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM devices WHERE device_cluster = ? AND device_id = ?",
            (device_cluster, device_id),
        )
        conn.commit()
    finally:
        conn.close()

def get_device_address(device_cluster: str, device_id: str) -> tuple[str, int] | None:
    """Look up device (ip, port) by cluster and device id. Returns None if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT ip, port FROM devices WHERE device_cluster = ? AND device_id = ?",
            (device_cluster, device_id),
        ).fetchone()
        if not row:
            return None
        return (row["ip"], row["port"] if row["port"] is not None else 9090)
    finally:
        conn.close()


def get_device_ip(device_cluster: str, device_id: str) -> str | None:
    """Look up device IP by cluster and device id. Returns None if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT ip FROM devices WHERE device_cluster = ? AND device_id = ?",
            (device_cluster, device_id),
        ).fetchone()
        return row["ip"] if row else None
    finally:
        conn.close()


def update_heartbeat(
    device_cluster: str,
    device_id: str,
    current_model: str | None = None,
    status: HeartbeatStatus | str | None = None,
) -> None:
    """Upsert a heartbeat into device_status. Validates status against HeartbeatStatus."""
    if status is not None:
        status_val = HeartbeatStatus(status) if isinstance(status, str) else status
        status_str = str(status_val.value)
    else:
        status_str = None

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO device_status (device_cluster, device_id, current_model, status, last_heartbeat)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(device_cluster, device_id) DO UPDATE SET
                current_model = COALESCE(excluded.current_model, current_model),
                status = COALESCE(excluded.status, status),
                last_heartbeat = CURRENT_TIMESTAMP
            """,
            (device_cluster, device_id, current_model, status_str),
        )
        conn.commit()
    finally:
        conn.close()

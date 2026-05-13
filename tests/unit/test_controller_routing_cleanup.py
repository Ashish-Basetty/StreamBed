"""Controller DB: routing rows removed when a device is deleted from the deployment path."""
import sys
from pathlib import Path

import pytest

_CONTROLLER_NODE = Path(__file__).resolve().parents[2] / "control-plane" / "ControllerNode"
sys.path.insert(0, str(_CONTROLLER_NODE))


@pytest.fixture
def controller_db(monkeypatch, tmp_path):
    import streambed_controller.db as controller_db_mod

    monkeypatch.setattr(controller_db_mod, "DB_PATH", tmp_path / "controller_test.db")
    controller_db_mod.init_db()
    return controller_db_mod


def _count_routing(conn) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM routing").fetchone()
    return int(row["n"])


def test_delete_routing_involving_device_as_source(controller_db):
    conn = controller_db.get_connection()
    conn.execute(
        """INSERT INTO routing (source_cluster, source_device, target_cluster, target_device)
           VALUES ('c1', 'edge-a', 'c1', 'srv-1')"""
    )
    conn.execute(
        """INSERT INTO routing (source_cluster, source_device, target_cluster, target_device)
           VALUES ('c1', 'edge-b', 'c1', 'srv-2')"""
    )
    conn.commit()
    conn.close()

    n = controller_db.delete_routing_involving_device("c1", "edge-a")
    assert n == 1

    conn = controller_db.get_connection()
    assert _count_routing(conn) == 1
    row = conn.execute(
        "SELECT source_device FROM routing WHERE source_cluster = 'c1'"
    ).fetchone()
    assert row["source_device"] == "edge-b"
    conn.close()


def test_delete_routing_involving_device_as_target(controller_db):
    conn = controller_db.get_connection()
    conn.execute(
        """INSERT INTO routing (source_cluster, source_device, target_cluster, target_device)
           VALUES ('c1', 'edge-a', 'c1', 'srv-1')"""
    )
    conn.execute(
        """INSERT INTO routing (source_cluster, source_device, target_cluster, target_device)
           VALUES ('c1', 'edge-b', 'c1', 'srv-1')"""
    )
    conn.commit()
    conn.close()

    n = controller_db.delete_routing_involving_device("c1", "srv-1")
    assert n == 2

    conn = controller_db.get_connection()
    assert _count_routing(conn) == 0
    conn.close()

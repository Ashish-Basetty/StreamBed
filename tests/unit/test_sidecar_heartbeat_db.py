"""DB helpers introduced by the sidecar-heartbeat refactor:
update_sidecar_heartbeat, bootstrap_sidecar_endpoint, get_sidecar_endpoint.

Mirrors the fixture pattern from test_controller_routing_cleanup.py:
monkeypatch DB_PATH to a tmp file and init_db() per test.
"""
import sys
import time
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


# ---------- update_sidecar_heartbeat ----------


def test_first_heartbeat_returns_unknown(controller_db):
    state, prior = controller_db.update_sidecar_heartbeat(
        "c1", "server-001", "10.0.0.5", 7100, 0
    )
    assert state == "unknown"
    assert prior is None


def test_equal_dg_total_returns_idle(controller_db):
    controller_db.update_sidecar_heartbeat("c1", "server-001", "10.0.0.5", 7100, 42)
    state, prior = controller_db.update_sidecar_heartbeat(
        "c1", "server-001", "10.0.0.5", 7100, 42
    )
    assert state == "idle"
    assert prior == ("10.0.0.5", 7100)


def test_increasing_dg_total_returns_flowing(controller_db):
    controller_db.update_sidecar_heartbeat("c1", "server-001", "10.0.0.5", 7100, 100)
    state, _ = controller_db.update_sidecar_heartbeat(
        "c1", "server-001", "10.0.0.5", 7100, 101
    )
    assert state == "flowing"


def test_prior_endpoint_reflects_prev_value_not_current(controller_db):
    # First write sets the baseline.
    controller_db.update_sidecar_heartbeat("c1", "server-001", "10.0.0.5", 7100, 5)
    # Second write changes the endpoint — prior_endpoint should still be the OLD one
    # so the caller can detect the change and push.
    _, prior = controller_db.update_sidecar_heartbeat(
        "c1", "server-001", "10.0.0.6", 7200, 10
    )
    assert prior == ("10.0.0.5", 7100)


def test_status_set_active_after_heartbeat(controller_db):
    controller_db.update_sidecar_heartbeat("c1", "server-001", "10.0.0.5", 7100, 0)
    conn = controller_db.get_connection()
    try:
        row = conn.execute(
            "SELECT status FROM device_status WHERE device_cluster='c1' AND device_id='server-001'"
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "Active"


def test_last_heartbeat_refreshes_on_each_call(controller_db):
    controller_db.update_sidecar_heartbeat("c1", "server-001", "10.0.0.5", 7100, 0)
    conn = controller_db.get_connection()
    first_hb = conn.execute(
        "SELECT last_heartbeat FROM device_status WHERE device_cluster='c1' AND device_id='server-001'"
    ).fetchone()["last_heartbeat"]
    conn.close()

    # SQLite's CURRENT_TIMESTAMP has 1s resolution; sleep just past it.
    time.sleep(1.1)
    controller_db.update_sidecar_heartbeat("c1", "server-001", "10.0.0.5", 7100, 1)

    conn = controller_db.get_connection()
    second_hb = conn.execute(
        "SELECT last_heartbeat FROM device_status WHERE device_cluster='c1' AND device_id='server-001'"
    ).fetchone()["last_heartbeat"]
    conn.close()
    assert second_hb > first_hb


# ---------- bootstrap_sidecar_endpoint ----------


def test_bootstrap_inserts_fresh_row(controller_db):
    controller_db.bootstrap_sidecar_endpoint("c1", "server-001", "10.0.0.5", 7100)
    assert controller_db.get_sidecar_endpoint("c1", "server-001") == ("10.0.0.5", 7100)


def test_bootstrap_leaves_dg_total_null(controller_db):
    controller_db.bootstrap_sidecar_endpoint("c1", "server-001", "10.0.0.5", 7100)
    conn = controller_db.get_connection()
    try:
        row = conn.execute(
            "SELECT dg_total FROM device_status WHERE device_cluster='c1' AND device_id='server-001'"
        ).fetchone()
    finally:
        conn.close()
    assert row["dg_total"] is None


def test_bootstrap_updates_endpoint_without_clobbering_counters(controller_db):
    # Simulate: heartbeat already happened, dg_total and status are populated.
    controller_db.update_sidecar_heartbeat("c1", "server-001", "10.0.0.5", 7100, 99)
    controller_db.update_sidecar_heartbeat("c1", "server-001", "10.0.0.5", 7100, 105)

    # Now a re-deploy triggers a bootstrap with a new endpoint.
    controller_db.bootstrap_sidecar_endpoint("c1", "server-001", "10.0.0.6", 7200)

    conn = controller_db.get_connection()
    try:
        row = conn.execute(
            """SELECT sidecar_host_ip, sidecar_host_port, dg_total, status
               FROM device_status WHERE device_cluster='c1' AND device_id='server-001'"""
        ).fetchone()
    finally:
        conn.close()
    assert (row["sidecar_host_ip"], row["sidecar_host_port"]) == ("10.0.0.6", 7200)
    # dg_total and status survive — only the endpoint is being bootstrapped.
    assert row["dg_total"] == 105
    assert row["status"] == "Active"


# ---------- get_sidecar_endpoint ----------


def test_get_endpoint_missing_row_returns_none(controller_db):
    assert controller_db.get_sidecar_endpoint("c1", "nope") is None


def test_get_endpoint_null_fields_returns_none(controller_db):
    # Manually insert a row with null endpoint fields (e.g. before bootstrap).
    conn = controller_db.get_connection()
    conn.execute(
        "INSERT INTO device_status (device_cluster, device_id) VALUES (?, ?)",
        ("c1", "server-001"),
    )
    conn.commit()
    conn.close()
    assert controller_db.get_sidecar_endpoint("c1", "server-001") is None


def test_get_endpoint_populated_returns_tuple(controller_db):
    controller_db.bootstrap_sidecar_endpoint("c1", "server-001", "10.0.0.5", 7100)
    assert controller_db.get_sidecar_endpoint("c1", "server-001") == ("10.0.0.5", 7100)

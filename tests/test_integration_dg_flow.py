"""Local docker-compose flow-correctness test: assert `dg_total` grows on
both an edge and a server after the inference stack is up.

Exists to catch "no frames flowing" regressions before they reach GCP. The
May 11 `STREAM_PROXY_HOST` -> `SIDECAR_HOST` rename broke the edge<->daemon
env-var contract and the GCP `test_dg_total_grows_on_both_sides` test had to
catch it because nothing local asserted on actual frame flow.
"""
from __future__ import annotations

import time

import httpx
import pytest

from tests.deploy_utils import delete_device

pytestmark = [pytest.mark.integration, pytest.mark.integration_docker]


CONTROLLER_URL = "http://localhost:8080"
CLUSTER = "default"
# The session fixture deploys 5 PyTorch containers (server-001/002 +
# edge-001/002/003). We only need two edges flowing; drop edge-003 before
# warmup to reduce host memory pressure (PyTorch model load is the OOM
# driver) and assert flow on edge-001 and edge-002 plus whichever servers
# they get load-balanced onto.
EDGES_UNDER_TEST = ("edge-001", "edge-002")
EDGE_TO_DELETE = "edge-003"
ALL_DEVICES = ("server-001", "server-002", "edge-001", "edge-002", "edge-003")

WARMUP_TIMEOUT_S = 120  # PyTorch load + first frames out of mock-video
ROUTING_TIMEOUT_S = 30  # Time for controller to load-balance the two edges
SAMPLE_INTERVAL_S = 10.0


def _status_by_device() -> dict[str, dict]:
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{CONTROLLER_URL}/status", params={"device_cluster": CLUSTER})
    resp.raise_for_status()
    return {r["device_id"]: r for r in resp.json().get("status", [])}


def _routing_by_edge() -> dict[str, str]:
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{CONTROLLER_URL}/routing", params={"device_cluster": CLUSTER})
    resp.raise_for_status()
    return {r["source_device"]: r["target_device"] for r in resp.json().get("routing", [])}


def _diag_snapshot(label: str) -> None:
    """Compact per-device snapshot of the fields this test gates on, for triage."""
    try:
        status = _status_by_device()
    except Exception as e:
        print(f"\n[diag:{label}] /status query failed: {e}")
        return
    try:
        routing = _routing_by_edge()
    except Exception as e:
        print(f"\n[diag:{label}] /routing query failed: {e}")
        routing = {}
    print(f"\n[diag:{label}] device status (routing={routing}):")
    for d in ALL_DEVICES:
        row = status.get(d) or {}
        if not row:
            print(f"  {d}: <missing from /status>")
            continue
        print(
            f"  {d}: state={row.get('data_flow_state')!r} "
            f"dg_total={row.get('dg_total')} "
            f"sidecar_host_port={row.get('sidecar_host_port')} "
            f"inference_state={row.get('inference_state')!r} "
            f"last_heartbeat_age_s={row.get('last_heartbeat_age_s')}"
        )


def _wait_until(predicate, *, timeout_s: float, interval_s: float = 2.0, what: str = ""):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    _diag_snapshot("wait-timeout")
    pytest.fail(f"timed out after {timeout_s}s waiting for {what or 'condition'}; last={last!r}")


@pytest.mark.skip(reason="local dg flow test is OOM-prone; skipped pending host memory fix")
def test_dg_total_grows_locally(deployed_inference_stack) -> None:
    # Drop edge-003 before warmup so only one extra edge competes with
    # edge-001/edge-002 for host RAM during PyTorch load.
    try:
        delete_device(EDGE_TO_DELETE, controller_url=CONTROLLER_URL)
        print(f"\n[setup] deleted {EDGE_TO_DELETE} to reduce host memory pressure")
    except Exception as e:
        print(f"\n[setup] delete {EDGE_TO_DELETE} failed (continuing): {e}")
    _diag_snapshot("post-delete")

    # Wait for the controller to load-balance edge-001 and edge-002 onto
    # servers so we know which downstream servers to gate on.
    def edges_routed() -> dict[str, str] | None:
        routing = _routing_by_edge()
        if all(routing.get(e) for e in EDGES_UNDER_TEST):
            return {e: routing[e] for e in EDGES_UNDER_TEST}
        return None

    edge_to_server = _wait_until(
        edges_routed,
        timeout_s=ROUTING_TIMEOUT_S,
        interval_s=1,
        what="edge-001 and edge-002 to be assigned target servers",
    )
    print(f"\n[setup] routing resolved: {edge_to_server}")
    devices_under_test = sorted(set(EDGES_UNDER_TEST) | set(edge_to_server.values()))
    print(f"[setup] devices under test: {devices_under_test}")

    # Gate on `data_flow_state == "flowing"` rather than `dg_total > 0`.
    # The controller sets state=flowing only when the *current* heartbeat's
    # dg_total > the previous one — so a passing gate proves real-time
    # growth, not just a stale handshake counter (~1 datagram from the QUIC
    # setup alone passes the > 0 check long before PyTorch finishes loading).
    def sidecars_flowing() -> bool:
        status = _status_by_device()
        for d in devices_under_test:
            row = status.get(d) or {}
            if not row.get("sidecar_host_port"):
                return False
            if row.get("data_flow_state") != "flowing":
                return False
        return True

    _wait_until(
        sidecars_flowing,
        timeout_s=WARMUP_TIMEOUT_S,
        interval_s=2,
        what=f"sidecars on {devices_under_test} to report data_flow_state=flowing",
    )
    _diag_snapshot("flowing")

    def _dg_totals() -> dict[str, int]:
        status = _status_by_device()
        return {
            d: int(status.get(d, {}).get("dg_total") or 0)
            for d in devices_under_test
        }

    before = _dg_totals()
    time.sleep(SAMPLE_INTERVAL_S)
    after = _dg_totals()

    delta = {d: after[d] - before[d] for d in before}
    print(f"\n[sample] dg_total before={before} after={after} delta={delta}")

    failures = [d for d, v in delta.items() if v <= 0]
    if failures:
        _diag_snapshot("assert-failure")
    for device, d in delta.items():
        assert d > 0, (
            f"{device} dg_total did not grow over {SAMPLE_INTERVAL_S}s "
            f"(before={before[device]}, after={after[device]}, "
            f"routing={edge_to_server}); "
            f"local flow may be broken (env-var rename, mock-video unreachable, "
            f"sidecar misrouted)"
        )

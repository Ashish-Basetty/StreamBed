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

pytestmark = [pytest.mark.integration, pytest.mark.integration_docker]


CONTROLLER_URL = "http://localhost:8080"
CLUSTER = "default"
EDGE_ID = "edge-001"
SERVER_ID = "server-001"

WARMUP_TIMEOUT_S = 120  # PyTorch load + first frames out of mock-video
SAMPLE_INTERVAL_S = 10.0


def _status_by_device() -> dict[str, dict]:
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{CONTROLLER_URL}/status", params={"device_cluster": CLUSTER})
    resp.raise_for_status()
    return {r["device_id"]: r for r in resp.json().get("status", [])}


def _wait_until(predicate, *, timeout_s: float, interval_s: float = 2.0, what: str = ""):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    pytest.fail(f"timed out after {timeout_s}s waiting for {what or 'condition'}; last={last!r}")


def test_dg_total_grows_locally(deployed_inference_stack) -> None:
    # Gate on `data_flow_state == "flowing"` rather than `dg_total > 0`.
    # The controller sets state=flowing only when the *current* heartbeat's
    # dg_total > the previous one — so a passing gate proves real-time
    # growth, not just a stale handshake counter (~1 datagram from the QUIC
    # setup alone passes the > 0 check long before PyTorch finishes loading).
    def sidecars_flowing() -> bool:
        status = _status_by_device()
        for d in (EDGE_ID, SERVER_ID):
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
        what="both sidecars to report data_flow_state=flowing",
    )

    def _dg_totals() -> dict[str, int]:
        status = _status_by_device()
        return {
            d: int(status.get(d, {}).get("dg_total") or 0)
            for d in (EDGE_ID, SERVER_ID)
        }

    before = _dg_totals()
    time.sleep(SAMPLE_INTERVAL_S)
    after = _dg_totals()

    delta = {d: after[d] - before[d] for d in before}
    for device, d in delta.items():
        assert d > 0, (
            f"{device} dg_total did not grow over {SAMPLE_INTERVAL_S}s "
            f"(before={before[device]}, after={after[device]}); "
            f"local flow may be broken (env-var rename, mock-video unreachable, "
            f"sidecar misrouted)"
        )

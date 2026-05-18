"""ACTN flow over the real VPC: deploy server-01 + edge-01 with the
controller-hosted mock-video source; assert `dg_total` grows on both sides
over a 5-second sampling window.

This is a flow-correctness test, not a latency benchmark. Catches "packets
are not flowing" regressions (sidecar misroute, firewall closed, mock-video
not reachable). Does NOT measure per-packet RTT — that needs sidecar-level
timestamping and is out of scope for this suite.
"""
from __future__ import annotations

import time

import pytest

from tests.gcp.conftest import GCP_CLUSTER
from tests.gcp.deploy_helpers import (
    deployed_edge_server_pair,
    get_status,
    wait_until,
)

WARMUP_S = 90  # PyTorch container load on e2-small + frame pipeline warmup
SAMPLE_INTERVAL_S = 10.0


@pytest.mark.gcp
def test_dg_total_grows_on_both_sides(gcp_controller_reachable: str) -> None:
    url = gcp_controller_reachable
    with deployed_edge_server_pair(url, GCP_CLUSTER):
        # Wait for sidecar endpoints to populate before sampling counters;
        # without endpoints there's no flow possible.
        def sidecars_endpointed() -> bool:
            status = {r["device_id"]: r for r in get_status(url, GCP_CLUSTER)}
            return all(
                status.get(d, {}).get("sidecar_host_port")
                for d in ("edge-01", "server-01")
            )

        wait_until(sidecars_endpointed, timeout_s=90, interval_s=2, what="sidecars endpointed")
        time.sleep(WARMUP_S)

        def _dg_totals() -> dict[str, int]:
            status = {r["device_id"]: r for r in get_status(url, GCP_CLUSTER)}
            return {
                d: int(status.get(d, {}).get("dg_total") or 0)
                for d in ("edge-01", "server-01")
            }

        before = _dg_totals()
        time.sleep(SAMPLE_INTERVAL_S)
        after = _dg_totals()

        delta = {d: after[d] - before[d] for d in before}
        for device, d in delta.items():
            assert d > 0, (
                f"{device} dg_total did not grow over {SAMPLE_INTERVAL_S}s "
                f"(before={before[device]}, after={after[device]}); "
                f"flow may be broken (mock-video unreachable, sidecar misrouted, "
                f"or firewall blocking QUIC)"
            )

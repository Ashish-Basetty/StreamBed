"""Cross-VM QUIC handshake: deploy server-01 + edge-01, assert their sidecars
spawn and the controller sees both endpoints populated with VPC internal IPs.

This proves: daemon→sidecar spawn works on real VMs, host-port publish works
under the per-VM bridge layout, and the controller's endpoint-exchange
(telling the edge sidecar where the server sidecar listens) crosses the VPC.
"""
from __future__ import annotations

import ipaddress

import pytest

from tests.gcp.conftest import GCP_CLUSTER
from tests.gcp.deploy_helpers import (
    deployed_edge_server_pair,
    get_status,
    wait_until,
)

VPC_SUBNET = ipaddress.ip_network("10.10.0.0/24")
HANDSHAKE_TIMEOUT_S = 90
# Once endpoints are up, give the sidecars + inference stack time to start
# pushing datagrams. PyTorch container load on e2-small dominates this window;
# raise if observed flakier than ~3min on real infra.
FLOW_TIMEOUT_S = 180


@pytest.mark.gcp
def test_quic_handshake_completes_across_vpc(gcp_controller_reachable: str) -> None:
    url = gcp_controller_reachable
    with deployed_edge_server_pair(url, GCP_CLUSTER):
        def both_sidecars_ready() -> dict[str, dict] | None:
            """Both sidecars must have published their host endpoint."""
            status = {r["device_id"]: r for r in get_status(url, GCP_CLUSTER)}
            edge = status.get("edge-01")
            server = status.get("server-01")
            if not edge or not server:
                return None
            if not (edge.get("sidecar_host_ip") and edge.get("sidecar_host_port")):
                return None
            if not (server.get("sidecar_host_ip") and server.get("sidecar_host_port")):
                return None
            return {"edge": edge, "server": server}

        ready = wait_until(
            both_sidecars_ready,
            timeout_s=HANDSHAKE_TIMEOUT_S,
            interval_s=2,
            what="both sidecars to publish host endpoint",
        )

        # Endpoint allocation alone is a false-positive trap: the daemon can
        # reserve a port even when the sidecar container fails to spawn (we
        # hit this with the arm64-only sidecar image). Require that both
        # sidecars are actually alive AND moving frames — a positive
        # `dg_total` proves the QUIC layer is end-to-end functional, not
        # just that ports were reserved.
        def both_sidecars_flowing() -> bool:
            status = {r["device_id"]: r for r in get_status(url, GCP_CLUSTER)}
            for d in ("edge-01", "server-01"):
                row = status.get(d) or {}
                if not (row.get("dg_total") or 0) > 0:
                    return False
            return True

        wait_until(
            both_sidecars_flowing,
            timeout_s=FLOW_TIMEOUT_S,
            interval_s=2,
            what="both sidecars to report dg_total > 0",
        )

        for role, row in ready.items():
            ip = ipaddress.ip_address(row["sidecar_host_ip"])
            assert ip in VPC_SUBNET, (
                f"{role} sidecar_host_ip {ip} not in VPC {VPC_SUBNET}; "
                f"daemon may be falling back to a non-internal address"
            )
            port = int(row["sidecar_host_port"])
            assert 7001 <= port <= 7100, (
                f"{role} sidecar_host_port {port} outside expected range"
            )

"""Cross-VM device registration: all 4 workers register with the controller
and report their VPC internal IPs (not host.docker.internal)."""
from __future__ import annotations

import ipaddress

import httpx
import pytest

from tests.gcp.conftest import GCP_CLUSTER, GCP_WORKER_DEVICE_IDS


@pytest.mark.gcp
def test_all_workers_registered(gcp_controller_reachable: str) -> None:
    with httpx.Client(timeout=10) as client:
        resp = client.get(
            f"{gcp_controller_reachable}/devices",
            params={"device_cluster": GCP_CLUSTER},
        )
    resp.raise_for_status()
    registered = {d["device_id"] for d in resp.json().get("devices", [])}
    missing = set(GCP_WORKER_DEVICE_IDS) - registered
    assert not missing, (
        f"Missing daemons: {sorted(missing)}. Registered: {sorted(registered)}. "
        f"Check `sudo docker compose -f docker-compose.worker.yml ps` on each VM."
    )


@pytest.mark.gcp
def test_workers_report_vpc_internal_ips(gcp_controller_reachable: str) -> None:
    """DAEMON_PUBLIC_IP on each worker should be a 10.10.0.0/24 internal IP,
    not host.docker.internal or a public address. Proves startup.sh stamped
    the metadata-derived IP and the daemon picked it up."""
    expected_subnet = ipaddress.ip_network("10.10.0.0/24")
    with httpx.Client(timeout=10) as client:
        resp = client.get(
            f"{gcp_controller_reachable}/devices",
            params={"device_cluster": GCP_CLUSTER},
        )
    resp.raise_for_status()
    devices = resp.json().get("devices", [])
    for dev in devices:
        if dev["device_id"] not in GCP_WORKER_DEVICE_IDS:
            continue
        ip_str = dev.get("ip") or dev.get("public_ip") or dev.get("daemon_public_ip")
        assert ip_str, f"No IP field on device record: {dev}"
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            pytest.fail(f"{dev['device_id']} reported non-IP address: {ip_str!r}")
        assert ip in expected_subnet, (
            f"{dev['device_id']} reported {ip}, expected an address in {expected_subnet}"
        )

"""Startup-script regression: every VM's /etc/default/streambed has all
expected keys, and DAEMON_PUBLIC_IP is a real VPC address. Catches the
case where a metadata key got renamed or curl in startup.sh silently failed.

Runs via `gcloud compute ssh --tunnel-through-iap` — slow but only on
demand.
"""
from __future__ import annotations

import ipaddress
import shutil
import subprocess

import pytest

from tests.gcp.conftest import GCP_WORKER_DEVICE_IDS

REQUIRED_KEYS = {
    "DEVICE_ID",
    "DEVICE_TYPE",
    "DEVICE_CLUSTER",
    "CONTROLLER_URL",
    "DAEMON_PUBLIC_IP",
    "DAEMON_PUBLIC_PORT",
    "SIDECAR_PORT_RANGE_MIN",
    "SIDECAR_PORT_RANGE_MAX",
    "DEVICE_NETWORK_NAME",
}
EXPECTED_SUBNET = ipaddress.ip_network("10.10.0.0/24")


def _ssh_cat(vm_name: str) -> str:
    if not shutil.which("gcloud"):
        pytest.skip("gcloud CLI not on PATH")
    result = subprocess.run(
        [
            "gcloud", "compute", "ssh", vm_name,
            "--zone=us-central1-a",
            "--tunnel-through-iap",
            "--command=cat /etc/default/streambed",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(f"ssh {vm_name} failed: {result.stderr.strip()}")
    return result.stdout


def _parse_env(content: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


@pytest.mark.gcp
@pytest.mark.parametrize("vm_name", GCP_WORKER_DEVICE_IDS)
def test_etc_default_streambed_complete(vm_name: str) -> None:
    env = _parse_env(_ssh_cat(vm_name))
    missing = REQUIRED_KEYS - env.keys()
    assert not missing, f"{vm_name} /etc/default/streambed missing keys: {sorted(missing)}"
    ip = ipaddress.ip_address(env["DAEMON_PUBLIC_IP"])
    assert ip in EXPECTED_SUBNET, (
        f"{vm_name} DAEMON_PUBLIC_IP={ip} not in expected subnet {EXPECTED_SUBNET}"
    )

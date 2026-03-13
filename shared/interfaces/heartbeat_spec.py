"""Heartbeat status specification for StreamBed controller.

Add new status values here as needed; the SQLite device_status table
stores these as strings and validates against this enum.
"""
from enum import StrEnum


class HeartbeatStatus(StrEnum):
    """Valid heartbeat status values reported by devices."""

    ACTIVE = "Active"
    UNRESPONSIVE = "Unresponsive"
    DEPLOYMENT_FAILURE = "Deployment Failure"
    UNKNOWN = "Unknown"

"""Server feedback based bandwidth estimator."""

from typing import Any

from shared.bandwidth.base import BandwidthEstimator


class ServerFeedbackBackend(BandwidthEstimator):
    """
    Receives received_bps via UDP push from server. Use it as target (or cap).
    Updates via update_from_response() when daemon receives feedback packet.
    """

    def __init__(self, default_bps: float = 500_000):
        self._default_bps = default_bps
        self._target_bps = default_bps

    def get_target_bps(self) -> float:
        return self._target_bps

    def update_from_response(self, data: dict[str, Any]) -> None:
        """Update target from a feedback dict (UDP packet or test injection)."""
        received_bps = data.get("received_bps")
        if received_bps is not None and isinstance(received_bps, (int, float)):
            self._target_bps = float(received_bps)
        else:
            self._target_bps = self._default_bps

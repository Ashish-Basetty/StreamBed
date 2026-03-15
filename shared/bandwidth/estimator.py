"""Bandwidth estimator backends: Config and SentRate."""

import time

from shared.bandwidth.base import BandwidthEstimator


class ConfigBackend(BandwidthEstimator):
    """Fixed target from config. No adaptation."""

    def __init__(self, initial_bps: float):
        self._target_bps = initial_bps

    def get_target_bps(self) -> float:
        return self._target_bps


class SentRateBackend(BandwidthEstimator):
    """
    Sample-based throughput estimator. One bucket per sample period.

    get_target_bps() acts as the sample trigger: compute throughput from the
    bytes accumulated since the last call, then reset. O(1) per on_bytes_sent
    and get_target_bps. Designed for a prescribed sample loop.
    """

    def __init__(
        self,
        safety_factor: float = 0.9,
        min_bps: float = 10_000,
        max_bps: float = 50_000_000,
        ewma_alpha: float = 0.2,
        initial_bps: float | None = None,
    ):
        self._safety_factor = safety_factor
        self._min_bps = min_bps
        self._max_bps = max_bps
        self._ewma_alpha = ewma_alpha
        self._smoothed_bps: float = initial_bps if initial_bps is not None else min_bps
        self._bytes_sent: int = 0
        self._sample_start_time: float = time.monotonic()

    def on_bytes_sent(self, n: int) -> None:
        self._bytes_sent += n

    def get_target_bps(self) -> float:
        now = time.monotonic()
        duration = now - self._sample_start_time

        # Reset for next sample
        self._sample_start_time = now
        bytes_this_sample = self._bytes_sent
        self._bytes_sent = 0

        if duration <= 0:
            return self._smoothed_bps

        if bytes_this_sample == 0:
            # Decay toward min when no sends this sample
            self._smoothed_bps = (
                self._ewma_alpha * self._min_bps
                + (1 - self._ewma_alpha) * self._smoothed_bps
            )
            return self._smoothed_bps

        achieved_bps = (bytes_this_sample * 8) / duration
        target = achieved_bps * self._safety_factor
        target = max(self._min_bps, min(self._max_bps, target))

        self._smoothed_bps = (
            self._ewma_alpha * target + (1 - self._ewma_alpha) * self._smoothed_bps
        )
        return self._smoothed_bps

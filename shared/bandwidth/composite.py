"""Composite bandwidth estimator - takes minimum of multiple backends."""

from shared.bandwidth.base import BandwidthEstimator


class CompositeBackend(BandwidthEstimator):
    """
    Take the minimum of several backends.
    Ensures we never exceed the most conservative estimate.
    """

    def __init__(self, *backends: BandwidthEstimator):
        self._backends = list(backends)

    def get_target_bps(self) -> float:
        if not self._backends:
            return 10_000  # fallback
        return min(b.get_target_bps() for b in self._backends)

    def on_bytes_sent(self, n: int) -> None:
        for b in self._backends:
            b.on_bytes_sent(n)

    def on_bytes_queued(self, n: int) -> None:
        for b in self._backends:
            b.on_bytes_queued(n)

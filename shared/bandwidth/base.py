"""Bandwidth estimator base class."""

from abc import ABC, abstractmethod


class BandwidthEstimator(ABC):
    """
    Base class for bandwidth estimator backends.

    Produces a dynamic target bits-per-second for stream throttling.
    Subclasses override get_target_bps() and optionally on_bytes_sent/on_bytes_queued.
    """

    @abstractmethod
    def get_target_bps(self) -> float:
        """Return current target bits per second for video stream."""
        ...

    def on_bytes_sent(self, n: int) -> None:
        """Notify that n bytes were sent. Override for sent-rate backends."""
        pass

    def on_bytes_queued(self, n: int) -> None:
        """Notify current queue depth in bytes. Override for queue-depth backends."""
        pass

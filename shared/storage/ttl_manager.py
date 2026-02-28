import shutil
from pathlib import Path


class TTLManager:
    """Calculates dynamic TTL for cached data based on remaining disk space."""

    def __init__(
        self,
        storage_path: str,
        max_ttl: float = 3600.0,
        min_ttl: float = 30.0,
        critical_pct: float = 0.10,
    ):
        """
        Args:
            storage_path: Path to the storage volume to monitor.
            max_ttl: Maximum TTL in seconds when disk is plentiful.
            min_ttl: Minimum TTL in seconds when disk is critically low.
            critical_pct: Fraction of free space considered critical (e.g. 0.10 = 10%).
        """
        self._path = Path(storage_path)
        self._max_ttl = max_ttl
        self._min_ttl = min_ttl
        self._critical_pct = critical_pct

    def get_disk_usage(self) -> tuple[float, float]:
        """Returns (fraction_used, fraction_free) for the storage volume."""
        usage = shutil.disk_usage(self._path)
        frac_free = usage.free / usage.total
        return (1.0 - frac_free, frac_free)

    def compute_ttl(self) -> float:
        """Return TTL in seconds, linearly scaled between max and min
        as free space drops from 100% to critical_pct."""
        _, frac_free = self.get_disk_usage()
        if frac_free <= self._critical_pct:
            return self._min_ttl
        scale = (frac_free - self._critical_pct) / (1.0 - self._critical_pct)
        return self._min_ttl + scale * (self._max_ttl - self._min_ttl)

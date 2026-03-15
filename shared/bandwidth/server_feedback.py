"""Server feedback based bandwidth estimator."""

import asyncio
import logging
from typing import Any

import httpx

from shared.bandwidth.base import BandwidthEstimator

logger = logging.getLogger(__name__)


class ServerFeedbackBackend(BandwidthEstimator):
    """
    Server reports received_bps over HTTP. Use it as target (or cap).
    Polls GET /api/v1/stream-feedback every poll_interval seconds.
    """

    def __init__(
        self,
        feedback_url: str,
        poll_interval: float = 2.0,
        default_bps: float = 500_000,
        timeout: float = 5.0,
    ):
        self._feedback_url = feedback_url.rstrip("/")
        if not self._feedback_url.endswith("/stream-feedback"):
            self._feedback_url = f"{self._feedback_url}/api/v1/stream-feedback"
        self._poll_interval = poll_interval
        self._default_bps = default_bps
        self._timeout = timeout
        self._target_bps = default_bps
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background poll loop."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop the background poll loop."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def get_target_bps(self) -> float:
        return self._target_bps

    def update_from_response(self, data: dict[str, Any]) -> None:
        """Update target from a response dict (e.g. for testing or manual injection)."""
        received_bps = data.get("received_bps")
        if received_bps is not None and isinstance(received_bps, (int, float)):
            self._target_bps = float(received_bps)
        else:
            self._target_bps = self._default_bps

    async def _poll_loop(self) -> None:
        while True:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.get(self._feedback_url)
                    resp.raise_for_status()
                    data: dict[str, Any] = resp.json()
                    received_bps = data.get("received_bps")
                    if received_bps is not None and isinstance(received_bps, (int, float)):
                        self._target_bps = float(received_bps)
            except Exception as e:
                logger.debug("Server feedback poll failed: %s, using default", e)
                self._target_bps = self._default_bps
            await asyncio.sleep(self._poll_interval)

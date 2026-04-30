"""Stream proxy manager for StreamBed daemon."""

import asyncio
import logging
import time

from shared.bandwidth import BandwidthEstimator
from shared.stream_chunks import make_chunks

from daemon_config import (
    MAX_VIDEO_FPS,
    SIDECAR_LOCAL_UDP_PORT,
    STREAM_TRANSPORT,
)

logger = logging.getLogger(__name__)


class StreamProxyManager:
    """Singleton manager for stream proxy state (target, UDP transport, drop logic)."""

    _instance: "StreamProxyManager | None" = None

    def __new__(cls) -> "StreamProxyManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._target: dict[str, str | int | None] = {"ip": None, "port": None}
        self._udp_transport: asyncio.DatagramTransport | None = None
        self._print_invalid_dest = False
        self._estimator: BandwidthEstimator | None = None
        self._target_frame_interval: float = 1.0 / MAX_VIDEO_FPS
        self._last_video_send_time: float = 0.0
        self._frame_size_alpha: float = 0.5
        self._avg_frame_size_bytes: float = 50_000

    def set_estimator(self, estimator: BandwidthEstimator) -> None:
        self._estimator = estimator

    def get_estimator(self) -> BandwidthEstimator | None:
        return self._estimator

    def set_udp_transport(self, transport: asyncio.DatagramTransport) -> None:
        self._udp_transport = transport

    def get_udp_transport(self) -> asyncio.DatagramTransport | None:
        return self._udp_transport

    def update_estimator_bytes_sent(self, n: int) -> None:
        """Notify the estimator of bytes sent. No-op if no estimator; swallows errors."""
        try:
            self._estimator.on_bytes_sent(n)
        except Exception:
            pass

    def set_target(self, ip: str, port: int) -> None:
        self._target["ip"] = ip
        self._target["port"] = port
        self._print_invalid_dest = False

    def get_target(self) -> tuple[str | None, int | None]:
        return self._target["ip"], self._target["port"]

    def should_drop_video_frame(self) -> bool:
        """Return True if we should drop this video frame (rate limit)."""
        if self._target_frame_interval <= 0:
            return False
        now = time.monotonic()
        if now - self._last_video_send_time < self._target_frame_interval:
            return True
        self._last_video_send_time = now
        return False

    def invalid_logged(self) -> bool:
        return self._print_invalid_dest

    def mark_invalid_logged(self) -> None:
        self._print_invalid_dest = True

    def reset_invalid_logged(self) -> None:
        self._print_invalid_dest = False

    def update_target_bitrate(self, bitrate: float) -> None:
        if bitrate <= 0:
            logger.warning("[Daemon] Invalid target bitrate %f, no update", bitrate)
            return
        calculated_frame_interval = (self._avg_frame_size_bytes * 8) / bitrate
        self._target_frame_interval = max(calculated_frame_interval, 1.0 / MAX_VIDEO_FPS)

    def forward_frame(self, payload: bytes, frame_len: int, embedding_len: int) -> None:
        """Decide whether to forward and send if so.

        Under STREAM_TRANSPORT=quic, chunks are written to the local sidecar at
        127.0.0.1:SIDECAR_LOCAL_UDP_PORT instead of directly to the peer.
        """
        if frame_len > 0 and self.should_drop_video_frame():
            return
        transport = self.get_udp_transport()
        if transport is None:
            return
        if STREAM_TRANSPORT == "quic":
            dest = ("127.0.0.1", SIDECAR_LOCAL_UDP_PORT)
        else:
            ip, port = self.get_target()
            if ip is None or port is None:
                if not self.invalid_logged():
                    logger.warning("[Daemon] No stream target, dropping frame")
                    self.mark_invalid_logged()
                return
            dest = (ip, port)
        self.reset_invalid_logged()
        for chunk in make_chunks(payload):
            transport.sendto(chunk, dest)
        self.update_estimator_bytes_sent(len(payload))
        self._avg_frame_size_bytes = (1 - self._frame_size_alpha) * self._avg_frame_size_bytes + self._frame_size_alpha * len(payload)

    def close(self) -> None:
        if self._udp_transport:
            self._udp_transport.close()
            self._udp_transport = None

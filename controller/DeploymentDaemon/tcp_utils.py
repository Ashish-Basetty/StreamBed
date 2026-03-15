"""TCP stream handling for StreamBed daemon."""

import asyncio
import logging
import struct

from stream_proxy_manager import StreamProxyManager

logger = logging.getLogger(__name__)


class _UDPSendOnlyProtocol(asyncio.DatagramProtocol):
    """Placeholder for send-only UDP transport. Extensible for server feedback later."""
    def datagram_received(self, data: bytes, addr: tuple) -> None:
        pass
    def error_received(self, exc: Exception) -> None:
        pass


async def handle_tcp_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    manager: StreamProxyManager,
    max_payload_bytes: int = 50_000_000,
) -> None:
    """Read length-prefixed frames, parse header, forward via manager for each."""
    addr = writer.get_extra_info("peername", "unknown")
    try:
        while True:
            len_buf = await reader.readexactly(4)
            if not len_buf:
                break
            payload_len = struct.unpack(">I", len_buf)[0]
            if payload_len <= 0 or payload_len > max_payload_bytes:
                logger.warning(f"Invalid frame length {payload_len} from {addr}, closing")
                break
            payload = await reader.readexactly(payload_len)
            if len(payload) < 32:
                continue
            frame_len, embedding_len = struct.unpack_from(">II", payload, 24)
            manager.forward_frame(payload, frame_len, embedding_len)
    except asyncio.IncompleteReadError:
        pass
    except Exception as e:
        logger.debug(f"TCP stream error from {addr}: {e}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

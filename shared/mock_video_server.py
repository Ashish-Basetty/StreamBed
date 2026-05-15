"""Mock TCP video server for testing.

Accepts connections from inference containers and streams synthetic (zero)
frames at a fixed FPS using the StreamBed chunk wire format.

Frame payload wire format:
    struct.pack(">HHH", H, W, C)  — 6-byte shape header
    raw uint8 frame bytes          — H * W * C bytes
"""
from __future__ import annotations

import asyncio
import struct
import threading

import numpy as np

from shared.tcp_framing import CHUNK_MAGIC, write_message

_SHAPE_FMT = ">HHH"  # big-endian uint16: H, W, C
SHAPE_HEADER_SIZE = struct.calcsize(_SHAPE_FMT)


class MockVideoServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9200,
        frame_shape: tuple[int, int, int] = (64, 64, 3),
        fps: float = 30.0,
    ):
        self.host = host
        self.port = port
        self.frame_shape = frame_shape
        self.fps = fps
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        try:
            await self._server.serve_forever()
        except asyncio.CancelledError:
            pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        h, w, c = self.frame_shape
        shape_header = struct.pack(_SHAPE_FMT, h, w, c)
        frame_bytes = np.zeros((h, w, c), dtype=np.uint8).tobytes()
        payload = shape_header + frame_bytes
        interval = 1.0 / self.fps
        try:
            while True:
                await write_message(writer, CHUNK_MAGIC, payload)
                await asyncio.sleep(interval)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def stop(self) -> None:
        if self._server and self._loop:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread:
            self._thread.join(timeout=5)


def _docker_host_address() -> str:
    """Return the address Docker containers use to reach the host machine."""
    import platform
    if platform.system() == "Darwin":
        return "host.docker.internal"
    # Linux: read docker0 bridge gateway from /proc/net/fib_trie or fall back
    try:
        import subprocess
        out = subprocess.check_output(
            ["docker", "network", "inspect", "bridge", "--format", "{{range .IPAM.Config}}{{.Gateway}}{{end}}"],
            text=True,
        ).strip()
        if out:
            return out
    except Exception:
        pass
    return "172.17.0.1"

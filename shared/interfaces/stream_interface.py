from abc import ABC, abstractmethod
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Callable, Optional

import asyncio
import json
import struct
import time
import io
import os

import cv2
import numpy as np


from shared.stream_chunks import CHUNK_MAGIC, make_chunks as _make_chunks_impl

JPEG_MAGIC = b'JPEG'


@dataclass
class StreamFrame:
    timestamp: float
    frame: Optional[np.ndarray]
    embedding: Optional[np.ndarray]
    model_version: str
    source_device_id: str
    frame_interleaving_rate: Optional[float] = None


def serialize_stream_frame(frame: StreamFrame, use_jpeg: bool = False) -> bytes:
    model_ver_bytes = frame.model_version.encode('utf-8')
    source_id_bytes = frame.source_device_id.encode('utf-8')

    frame_bytes = b''
    if frame.frame is not None:
        if use_jpeg:
            ok, buf = cv2.imencode('.jpg', frame.frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            frame_bytes = JPEG_MAGIC + buf.tobytes() if ok else b''
        else:
            buf = io.BytesIO()
            np.save(buf, frame.frame)
            frame_bytes = buf.getvalue()

    embedding_bytes = b''
    if frame.embedding is not None:
        buf = io.BytesIO()
        np.save(buf, frame.embedding)
        embedding_bytes = buf.getvalue()

    interleaving = frame.frame_interleaving_rate if frame.frame_interleaving_rate is not None else -1.0
    header = struct.pack('>dIIdII',
                         frame.timestamp,
                         len(model_ver_bytes),
                         len(source_id_bytes),
                         interleaving,
                         len(frame_bytes),
                         len(embedding_bytes))

    return header + model_ver_bytes + source_id_bytes + frame_bytes + embedding_bytes


def deserialize_stream_frame(data: bytes) -> StreamFrame:
    header_size = struct.calcsize('>dIIdII')
    if len(data) < header_size:
        raise ValueError("Data too short for header")

    header = data[:header_size]
    timestamp, model_ver_len, source_id_len, interleaving, frame_len, embedding_len = struct.unpack('>dIIdII', header)

    offset = header_size
    model_ver_bytes = data[offset:offset + model_ver_len]
    offset += model_ver_len
    source_id_bytes = data[offset:offset + source_id_len]
    offset += source_id_len
    frame_bytes = data[offset:offset + frame_len] if frame_len > 0 else b''
    offset += frame_len
    embedding_bytes = data[offset:offset + embedding_len] if embedding_len > 0 else b''

    model_version = model_ver_bytes.decode('utf-8')
    source_device_id = source_id_bytes.decode('utf-8')

    frame = None
    if frame_bytes:
        if frame_bytes[:4] == JPEG_MAGIC:
            arr = np.frombuffer(frame_bytes[4:], dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        else:
            buf = io.BytesIO(frame_bytes)
            frame = np.load(buf)

    embedding = None
    if embedding_bytes:
        buf = io.BytesIO(embedding_bytes)
        embedding = np.load(buf)

    frame_interleaving_rate = interleaving if interleaving >= 0 else None

    return StreamFrame(
        timestamp=timestamp,
        frame=frame,
        embedding=embedding,
        model_version=model_version,
        source_device_id=source_device_id,
        frame_interleaving_rate=frame_interleaving_rate,
    )


def _make_chunks(stream_id: bytes, payload: bytes) -> list:
    return _make_chunks_impl(payload, stream_id)


def _parse_chunk(data: bytes):
    stream_id = data[4:20]
    chunk_index, total_chunks, data_len = struct.unpack('>III', data[20:32])
    return stream_id, chunk_index, total_chunks, data[32:32 + data_len]


class StreamSenderInterface(ABC):
    @abstractmethod
    async def connect(self, server_host: str, server_port: int) -> None:
        ...

    @abstractmethod
    async def send(self, frame: StreamFrame) -> bool:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...


class StreamReceiverInterface(ABC):
    @abstractmethod
    async def listen(self, host: str, port: int) -> None:
        ...

    @abstractmethod
    async def receive_stream(self) -> AsyncIterator[StreamFrame]:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...


class StreamBedUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def error_received(self, exc):
        print(f"[StreamBedUDPProtocol] error: {exc}")

    def connection_lost(self, exc):
        pass


class StreamBedTCPSender(StreamSenderInterface):
    """Send StreamFrames over TCP as length-prefixed serialized payloads."""

    def __init__(self, use_jpeg: bool = False):
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._use_jpeg = use_jpeg

    async def connect(self, server_host: str, server_port: int) -> None:
        self._reader, self._writer = await asyncio.open_connection(server_host, server_port)
        print(f"[TCPSender] connected to {server_host}:{server_port}")

    async def send(self, frame: StreamFrame) -> bool:
        if not self._writer:
            raise RuntimeError("sender is not connected")
        try:
            payload = serialize_stream_frame(frame, self._use_jpeg)
            self._writer.write(struct.pack(">I", len(payload)) + payload)
            await self._writer.drain()
            return True
        except Exception as e:
            print(f"[TCPSender] failed to send frame: {e}")
            return False

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None


class StreamBedUDPSender(StreamSenderInterface):
    def __init__(self, chunk_delay: float = 0.0, use_jpeg: bool = False):
        self._transport = None
        self._protocol = None
        self._server_addr = None
        self._chunk_delay = chunk_delay
        self._use_jpeg = use_jpeg

    async def connect(self, server_host: str, server_port: int) -> None:
        loop = asyncio.get_running_loop()
        self._server_addr = (server_host, server_port)
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: StreamBedUDPProtocol(),
            remote_addr=self._server_addr,
        )
        handshake = json.dumps({"type": "handshake", "source": "sender"}).encode("utf-8")
        self._transport.sendto(handshake)
        print(f"[UDPSender] handshake sent to {self._server_addr}")

    async def send(self, frame: StreamFrame) -> bool:
        if not self._transport or not self._server_addr:
            raise RuntimeError("sender is not connected")
        try:
            payload = serialize_stream_frame(frame, self._use_jpeg)
            stream_id = os.urandom(16)
            for chunk in _make_chunks(stream_id, payload):
                self._transport.sendto(chunk)
                await asyncio.sleep(self._chunk_delay)
            return True
        except Exception as e:
            print(f"[UDPSender] failed to send frame: {e}")
            return False

    async def close(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None
            self._protocol = None


class StreamBedUDPReceiver(StreamReceiverInterface):
    def __init__(
        self,
        on_bytes_received: Optional[Callable[[int], None]] = None,
        on_datagram_received: Optional[Callable[[bytes, tuple], None]] = None,
    ):
        self._transport = None
        self._protocol = None
        self._queue: Optional[asyncio.Queue] = None
        self._stopped = False
        self._on_bytes_received = on_bytes_received
        self._on_datagram_received = on_datagram_received

    class _RecvProtocol(StreamBedUDPProtocol):
        def __init__(
            self,
            queue: asyncio.Queue,
            on_bytes_received: Optional[Callable[[int], None]] = None,
            on_datagram_received: Optional[Callable[[bytes, tuple], None]] = None,
        ):
            super().__init__()
            self._queue = queue
            self._reassembly: dict = {}
            self._on_bytes_received = on_bytes_received
            self._on_datagram_received = on_datagram_received

        def datagram_received(self, data: bytes, addr):
            if self._on_bytes_received:
                self._on_bytes_received(len(data))
            if self._on_datagram_received:
                self._on_datagram_received(data, addr)
            try:
                text = data.decode("utf-8")
                msg = json.loads(text)
                if msg.get("type") == "handshake":
                    print(f"[UDPReceiver] handshake from {addr}")
                    return
            except Exception:
                pass

            if data[:4] == CHUNK_MAGIC:
                try:
                    stream_id, chunk_index, total_chunks, chunk_data = _parse_chunk(data)
                    if stream_id not in self._reassembly:
                        self._reassembly[stream_id] = [None] * total_chunks
                    self._reassembly[stream_id][chunk_index] = chunk_data
                    if all(c is not None for c in self._reassembly[stream_id]):
                        payload = b''.join(self._reassembly.pop(stream_id))
                        frame = deserialize_stream_frame(payload)
                        asyncio.create_task(self._queue.put(frame))
                except Exception as e:
                    print(f"[UDPReceiver] chunk error: {e}")
                return

            try:
                frame = deserialize_stream_frame(data)
                if isinstance(frame, StreamFrame):
                    asyncio.create_task(self._queue.put(frame))
            except Exception as e:
                print(f"[UDPReceiver] unable to decode packet: {e}")

    async def listen(self, host: str, port: int) -> None:
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: StreamBedUDPReceiver._RecvProtocol(
                self._queue, self._on_bytes_received, self._on_datagram_received
            ),
            local_addr=(host, port),
        )
        print(f"[UDPReceiver] listening on {host}:{port}")

    def send_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Send a UDP packet (e.g. feedback to stream source)."""
        if self._transport:
            self._transport.sendto(data, addr)

    async def receive_stream(self) -> AsyncIterator[StreamFrame]:
        if self._queue is None:
            raise RuntimeError("receiver.listen must be called before receive_stream")
        while not self._stopped:
            frame = await self._queue.get()
            yield frame

    async def recv_one(self, timeout: float | None = None) -> StreamFrame | None:
        """Public dequeue for tests and adapters; returns None on timeout."""
        if self._queue is None:
            raise RuntimeError("receiver.listen must be called before recv_one")
        try:
            if timeout is None:
                return await self._queue.get()
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def get_local_port(self) -> int | None:
        """Public accessor for the receiver's bound port."""
        if self._transport is None:
            return None
        sock = self._transport.get_extra_info("socket")
        return sock.getsockname()[1] if sock else None

    def queue_size(self) -> int:
        return self._queue.qsize() if self._queue else 0

    async def stop(self) -> None:
        self._stopped = True
        if self._transport:
            self._transport.close()
            self._transport = None
            self._protocol = None
        if self._queue:
            while not self._queue.empty():
                _ = self._queue.get_nowait()
            self._queue = None


class StreamBedUDPServerReceiver(StreamBedUDPReceiver):
    """
    Server-side receiver that tracks bytes received and source addr for UDP feedback push.
    Inherits from StreamBedUDPReceiver; adds stream_received and stream_source_addr fields.
    """

    def __init__(self):
        self.stream_received: deque = deque(maxlen=5000)
        self.stream_source_addr: tuple | None = None
        super().__init__(on_datagram_received=self._on_datagram)

    def _on_datagram(self, data: bytes, addr: tuple) -> None:
        self.stream_received.append((time.monotonic(), len(data)))
        self.stream_source_addr = addr
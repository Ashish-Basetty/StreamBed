from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Optional

import asyncio
import json
import struct
import io
import math
import os

import cv2
import numpy as np


CHUNK_MAGIC = b'CHNK'
# todo: change to <1400 to avoid IP-level fragmentation, but need to handle more chunks per frame
CHUNK_SIZE = 8000
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
    n = max(1, math.ceil(len(payload) / CHUNK_SIZE))
    chunks = []
    for i in range(n):
        data = payload[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
        chunks.append(CHUNK_MAGIC + stream_id + struct.pack('>III', i, n, len(data)) + data)
    return chunks


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
    def __init__(self):
        self._transport = None
        self._protocol = None
        self._queue: Optional[asyncio.Queue] = None
        self._stopped = False

    class _RecvProtocol(StreamBedUDPProtocol):
        def __init__(self, queue: asyncio.Queue):
            super().__init__()
            self._queue = queue
            self._reassembly: dict = {}

        def datagram_received(self, data: bytes, addr):
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
            lambda: StreamBedUDPReceiver._RecvProtocol(self._queue),
            local_addr=(host, port),
        )
        print(f"[UDPReceiver] listening on {host}:{port}")

    async def receive_stream(self) -> AsyncIterator[StreamFrame]:
        if self._queue is None:
            raise RuntimeError("receiver.listen must be called before receive_stream")
        while not self._stopped:
            frame = await self._queue.get()
            yield frame

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


class MockStreamSender(StreamSenderInterface):
    async def connect(self, server_host, server_port):
        print(f"[MockSender] connect({server_host}:{server_port})")

    async def send(self, frame):
        return True

    async def close(self):
        pass


class MockStreamReceiver(StreamReceiverInterface):
    async def listen(self, host, port):
        print(f"[MockReceiver] listen({host}:{port})")

    async def receive_stream(self):
        return
        yield

    async def stop(self):
        pass

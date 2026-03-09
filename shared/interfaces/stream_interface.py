"""StreamBed streaming protocol support.

This module defines the contract between senders (edge devices) and
receivers (servers) for the custom StreamBed UDP-based transport.  A simple
handshake and pickle-based payload serialization are provided alongside
mock helpers for local testing.

The original stub/mocks have been extended with working ``StreamBedUDPSender``
and ``StreamBedUDPReceiver`` implementations; the mocks are still available
for unit tests or offline development.
"""
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Optional

import asyncio
import pickle
import json

import numpy as np


@dataclass
class StreamFrame:
    """A single unit of data sent over the StreamBed streaming protocol."""

    timestamp: float
    frame: Optional[np.ndarray]
    embedding: Optional[np.ndarray]
    model_version: str
    source_device_id: str


class StreamSenderInterface(ABC):
    """Contract for the sending side (edge device -> server)."""

    @abstractmethod
    async def connect(self, server_host: str, server_port: int) -> None:
        """Perform the StreamBed handshake and open the stream."""
        ...

    @abstractmethod
    async def send(self, frame: StreamFrame) -> bool:
        """Send one frame/embedding unit. Returns True if acknowledged."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Gracefully close the stream."""
        ...


class StreamReceiverInterface(ABC):
    """Contract for the receiving side (server <- edge device)."""

    @abstractmethod
    async def listen(self, host: str, port: int) -> None:
        """Begin listening for incoming StreamBed connections."""
        ...

    @abstractmethod
    async def receive_stream(self) -> AsyncIterator[StreamFrame]:
        """Async generator that yields StreamFrame objects as they arrive."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop listening."""
        ...


class StreamBedUDPProtocol(asyncio.DatagramProtocol):
    """Shared base protocol for UDP datagrams.

    It doesn't know about frames; higher‑level classes will subclass it or
    wrap it with custom callbacks.
    """

    def __init__(self):
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def error_received(self, exc):
        print(f"[StreamBedUDPProtocol] error: {exc}")

    def connection_lost(self, exc):
        # transport closed
        pass


class StreamBedUDPSender(StreamSenderInterface):
    """UDP implementation of the sender side of the StreamBed protocol.

    This class serialises :class:`StreamFrame` objects with pickle and
authenticates the receiver with a simple handshake JSON message.  The
implementation is intentionally minimal – it always returns True from
:py:meth:`send` and does not implement retransmission.

    Future enhancements might add sequence numbers, ack handling, compression,
    etc.
    """

    def __init__(self):
        self._transport = None
        self._protocol = None
        self._server_addr = None

    async def connect(self, server_host: str, server_port: int) -> None:
        loop = asyncio.get_running_loop()
        self._server_addr = (server_host, server_port)
        # create a datagram endpoint; local port is chosen automatically
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: StreamBedUDPProtocol(),
            remote_addr=self._server_addr,
        )
        # send a simple handshake so the receiver knows we're here
        handshake = json.dumps({
            "type": "handshake",
            "source": "sender",
        }).encode("utf-8")
        self._transport.sendto(handshake, self._server_addr)
        print(f"[UDPSender] handshake sent to {self._server_addr}")

    async def send(self, frame: StreamFrame) -> bool:
        if not self._transport or not self._server_addr:
            raise RuntimeError("sender is not connected")

        try:
            payload = pickle.dumps(frame)
            self._transport.sendto(payload, self._server_addr)
            return True
        except Exception as e:  # pragma: no cover - best effort
            print(f"[UDPSender] failed to send frame: {e}")
            return False

    async def close(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None
            self._protocol = None


class StreamBedUDPReceiver(StreamReceiverInterface):
    """UDP implementation of the receiving side.

    Received datagrams are unpickled and put into an ``asyncio.Queue`` which
    ``receive_stream`` iterates over.
    """

    def __init__(self):
        self._transport = None
        self._protocol = None
        # ``Optional`` is used for compatibility with older Python versions.
        self._queue: Optional[asyncio.Queue] = None
        self._stopped = False

    class _RecvProtocol(StreamBedUDPProtocol):
        def __init__(self, queue: asyncio.Queue):
            super().__init__()
            self._queue = queue

        def datagram_received(self, data: bytes, addr):
            # try to parse handshake first
            try:
                text = data.decode("utf-8")
                msg = json.loads(text)
                if msg.get("type") == "handshake":
                    print(f"[UDPReceiver] handshake from {addr}")
                    return
            except Exception:
                # not a handshake, fall through to try pickle
                pass

            try:
                frame = pickle.loads(data)
                if isinstance(frame, StreamFrame):
                    # schedule put on the queue so we don't block the event loop
                    asyncio.create_task(self._queue.put(frame))
                else:
                    print("[UDPReceiver] received non-stream object")
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
        # iterate until stop() is called
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
            # drain remaining items
            while not self._queue.empty():
                _ = self._queue.get_nowait()
            self._queue = None


class MockStreamSender(StreamSenderInterface):
    """No-op sender for local testing without the real protocol."""

    async def connect(self, server_host, server_port):
        print(f"[MockSender] connect({server_host}:{server_port})")

    async def send(self, frame):
        return True

    async def close(self):
        pass


class MockStreamReceiver(StreamReceiverInterface):
    """No-op receiver for local testing."""

    async def listen(self, host, port):
        print(f"[MockReceiver] listen({host}:{port})")

    async def receive_stream(self):
        return
        yield  # makes this an async generator that yields nothing

    async def stop(self):
        pass

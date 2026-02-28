"""Interface stub for the StreamBed Streaming Protocol (Vishnu + Fred's component).

BLOCKER: The actual UDP-based streaming protocol is not yet implemented.
This file defines the contract and provides mock implementations for development.
Once the protocol is ready, implement StreamSenderInterface/StreamReceiverInterface.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Optional

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

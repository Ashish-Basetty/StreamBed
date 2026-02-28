"""Interface stub for the StreamBed Controller (Ashish's component).

BLOCKER: The actual controller API is not yet implemented.
This file defines the contract and provides a MockController for development.
Once Ashish's controller is ready, implement ControllerInterface with real logic.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class ModelUpdate:
    """Represents a model update pushed from the StreamBed Controller."""

    model_name: str
    new_version: str
    weights_url: str
    checksum: str


class ControllerInterface(ABC):
    """Contract for interacting with the StreamBed Controller.

    Inference containers use this to:
    1. Register themselves on startup
    2. Poll for model update notifications
    3. Apply model updates (download + hot-swap weights)
    4. Report health/status heartbeats
    """

    @abstractmethod
    async def register(
        self, device_id: str, device_type: str, current_model_version: str
    ) -> bool:
        """Register this container with the controller."""
        ...

    @abstractmethod
    async def check_for_update(
        self, device_id: str, current_version: str
    ) -> Optional[ModelUpdate]:
        """Poll for a newer model version. Returns None if up to date."""
        ...

    @abstractmethod
    async def apply_update(self, update: ModelUpdate, model) -> bool:
        """Download new weights and hot-swap into the given BaseVisionModel."""
        ...

    @abstractmethod
    async def report_status(self, device_id: str, status: dict) -> None:
        """Send a status heartbeat to the controller."""
        ...


class MockController(ControllerInterface):
    """No-op implementation for development/testing without the real controller."""

    async def register(self, device_id, device_type, current_model_version):
        print(f"[MockController] register({device_id}, {device_type})")
        return True

    async def check_for_update(self, device_id, current_version):
        return None  # Always "up to date"

    async def apply_update(self, update, model):
        return False

    async def report_status(self, device_id, status):
        pass

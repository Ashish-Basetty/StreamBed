"""Controller interface for StreamBed. Integrates with Ashish's controller API."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import httpx


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


class RealController(ControllerInterface):
    """HTTP client for Ashish's StreamBed controller. Uses push-based model updates."""

    def __init__(self, base_url: str, device_cluster: str = "default"):
        self._base_url = base_url.rstrip("/")
        self._device_cluster = device_cluster

    async def register(
        self, device_id: str, device_type: str, current_model_version: str
    ) -> bool:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/register",
                json={
                    "device_cluster": self._device_cluster,
                    "device_id": device_id,
                    "device_type": device_type,
                    "current_model_version": current_model_version,
                },
            )
            resp.raise_for_status()
            return True

    async def check_for_update(
        self, device_id: str, current_version: str
    ) -> Optional[ModelUpdate]:
        return None  # Push model: controller initiates deploys, no polling

    async def apply_update(self, update: ModelUpdate, model) -> bool:
        return False  # Push model: controller deploys containers directly

    async def report_status(self, device_id: str, status: dict) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{self._base_url}/heartbeat",
                    json={
                        "device_cluster": self._device_cluster,
                        "device_id": device_id,
                        "current_model": status.get("current_model"),
                        "status": status.get("status"),
                    },
                )
        except Exception as e:
            print(f"[RealController] heartbeat failed: {e}")


def get_controller(
    controller_url: Optional[str] = None,
    device_cluster: str = "default",
) -> ControllerInterface:
    """Return RealController if controller_url is set, else MockController."""
    if controller_url:
        return RealController(controller_url, device_cluster)
    return MockController()

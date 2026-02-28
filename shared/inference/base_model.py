from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class InferenceResult:
    """Output of a single frame inference."""

    embedding: np.ndarray
    label: Optional[str] = None
    confidence: Optional[float] = None
    raw_output: Optional[np.ndarray] = None


class BaseVisionModel(ABC):
    """Abstract wrapper for vision model inference."""

    @abstractmethod
    def load(self, model_path: Optional[str] = None) -> None:
        """Load model weights. model_path=None uses default pretrained weights."""
        ...

    @abstractmethod
    def preprocess(self, frame: np.ndarray) -> np.ndarray:
        """Convert a raw BGR/RGB frame (H,W,C) into model-ready input."""
        ...

    @abstractmethod
    def infer(self, preprocessed: np.ndarray) -> InferenceResult:
        """Run inference on a single preprocessed input."""
        ...

    def process_frame(self, frame: np.ndarray) -> InferenceResult:
        """Convenience: preprocess + infer in one call."""
        return self.infer(self.preprocess(frame))

    @abstractmethod
    def get_model_version(self) -> str:
        """Return a version string for the currently loaded model."""
        ...

from typing import Optional

import numpy as np
import torch
import torchvision.transforms as T
from torchvision.models import MobileNet_V2_Weights, mobilenet_v2

from .base_model import BaseVisionModel, InferenceResult


class MobileNetV2Model(BaseVisionModel):
    """MobileNetV2 wrapper — lightweight, suitable for edge simulation."""

    def __init__(self, device: str = "cpu"):
        self._device = torch.device(device)
        self._model = None
        self._feature_model = None
        self._transform = T.Compose([
            T.ToPILImage(),
            T.Resize(256),
            T.CenterCrop(224),
            T.ToTensor(),
            T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])
        self._version = "mobilenet_v2_imagenet_v2"
        self._labels: Optional[list[str]] = None

    def load(self, model_path: Optional[str] = None) -> None:
        weights = MobileNet_V2_Weights.DEFAULT
        self._labels = weights.meta["categories"]
        if model_path:
            self._model = mobilenet_v2()
            self._model.load_state_dict(
                torch.load(model_path, map_location=self._device)
            )
        else:
            self._model = mobilenet_v2(weights=weights)
        self._model.to(self._device).eval()

        # Feature extractor: everything except the final classifier
        self._feature_model = torch.nn.Sequential(
            *list(self._model.features.children()),
            torch.nn.AdaptiveAvgPool2d((1, 1)),
            torch.nn.Flatten(),
        )
        self._feature_model.to(self._device).eval()

    def preprocess(self, frame: np.ndarray) -> np.ndarray:
        tensor = self._transform(frame)
        return tensor.unsqueeze(0).numpy()

    def infer(self, preprocessed: np.ndarray) -> InferenceResult:
        input_tensor = torch.from_numpy(preprocessed).to(self._device)
        with torch.no_grad():
            logits = self._model(input_tensor)
            probs = torch.softmax(logits, dim=1)
            confidence, idx = probs.max(dim=1)
            embedding = self._feature_model(input_tensor).cpu().numpy().squeeze()

        label = self._labels[idx.item()] if self._labels else str(idx.item())
        return InferenceResult(
            embedding=embedding,
            label=label,
            confidence=confidence.item(),
            raw_output=logits.cpu().numpy(),
        )

    def get_model_version(self) -> str:
        return self._version

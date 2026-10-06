"""Blind image-classification jobs executed by vehicle workers.

The requester owns the ground truth.  This module deliberately accepts only
an image path and returns a prediction; it has no concept of the source
image's class.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


MODEL_PATH = os.getenv("IMAGE_MODEL_PATH", "")
IMAGE_SIZE = (224, 224)
_model: Any | None = None
_model_lock = threading.Lock()
_inference_lock = threading.Lock()


def _load_model() -> Any:
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            if not MODEL_PATH:
                raise RuntimeError("IMAGE_MODEL_PATH is not configured")
            try:
                from keras.models import load_model
            except ImportError as exc:
                raise RuntimeError(
                    "Keras 3 is required for image.classification jobs"
                ) from exc
            model_path = Path(MODEL_PATH).expanduser()
            if not model_path.is_file():
                raise FileNotFoundError(f"Image classification model not found: {model_path}")
            _model = load_model(model_path, compile=False)
    return _model


def classification(image_path: str) -> dict[str, Any]:
    """Classify an image without receiving or returning its ground truth."""
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"Classification image not found: {path}")

    with Image.open(path) as image:
        image_array = np.asarray(image.convert("RGB").resize(IMAGE_SIZE), dtype=np.float32)
    batch = np.expand_dims(image_array, axis=0)

    model = _load_model()
    # A shared model is cached per vehicle process.  Serializing inference
    # keeps concurrent vehicle job threads safe across Keras backends.
    with _inference_lock:
        prediction = model.predict(batch, verbose=0)
    score = float(np.asarray(prediction).reshape(-1)[0])
    return {
        "image_id": path.name,
        "predicted_class": "damaged" if score >= 0.5 else "not_damaged",
        "score": score,
    }

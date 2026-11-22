"""How strong the emotion on a face is, offline, with the FER+ model.

The 35 MB ONNX model (emotion-ferplus-8, MIT licence, ONNX model zoo) is
downloaded once next to the face model (``<data>/models/face``) and runs on
the CPU through onnxruntime. It reads a 64×64 grey face and gives eight
emotions; a cover wants the face that is furthest from "neutral", with
surprise and laughter counting most.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core.vision.faces import Face, FaceCancelled, FaceError, Progress, _resize

MODEL_FILE = "emotion-ferplus-8.onnx"
MODEL_URL = ("https://media.githubusercontent.com/media/onnx/models/main/validated/vision/body_analysis/"
             "emotion_ferplus/model/emotion-ferplus-8.onnx")
MODEL_SHA256 = "a2a2ba6a335a3b29c21acb6272f962bd3d47f84952aaffa03b60986e04efa61c"
INPUT = 64

LABELS = ("neutral", "happiness", "surprise", "sadness", "anger", "disgust", "fear", "contempt")
LABEL_TEXT = {"neutral": "calm", "happiness": "happy", "surprise": "surprised", "sadness": "sad",
              "anger": "angry", "disgust": "disgusted", "fear": "scared", "contempt": "smirking"}
# How much each emotion is worth on a cover (a big laugh or shock sells the clip).
WEIGHTS = {"neutral": 0.0, "happiness": 1.0, "surprise": 1.0, "anger": 0.85, "fear": 0.8, "disgust": 0.65,
           "sadness": 0.45, "contempt": 0.4}


def model_path(models_dir: Path) -> Path:
    return Path(models_dir) / "face" / MODEL_FILE


def download_model(models_dir: Path, progress: Progress | None = None,
                   cancel: threading.Event | None = None) -> Path:
    from core.audio.download import DownloadCancelled, DownloadError, fetch

    path = model_path(models_dir)
    if path.is_file() and _sha256(path) == MODEL_SHA256:
        return path
    path.unlink(missing_ok=True)
    try:
        fetch(MODEL_URL, path, 35, "the emotion model", progress, cancel)
    except DownloadCancelled as exc:
        raise FaceCancelled() from exc
    except DownloadError as exc:
        raise FaceError(f"Could not download the emotion model: {exc}") from exc
    if _sha256(path) != MODEL_SHA256:
        path.unlink(missing_ok=True)
        raise FaceError("The downloaded emotion model is damaged. Try again.")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Emotion:
    probs: tuple[float, ...]  # in LABELS order, sums to 1

    @property
    def label(self) -> str:
        return LABELS[int(np.argmax(self.probs))]

    @property
    def strength(self) -> float:
        """0 (blank face) … 1 (a clear laugh or shock)."""
        return float(sum(WEIGHTS[name] * p for name, p in zip(LABELS, self.probs)))

    @property
    def text(self) -> str:
        """"surprised 82%" — the strongest non-neutral emotion."""
        best = max(range(1, len(LABELS)), key=lambda i: self.probs[i])
        return f"{LABEL_TEXT[LABELS[best]]} {self.probs[best]:.0%}"


def softmax(logits: np.ndarray) -> np.ndarray:
    z = np.asarray(logits, dtype=np.float64).ravel()
    z = np.exp(z - z.max())
    return z / z.sum()


def face_crop(rgb: np.ndarray, face: Face, margin: float = 0.12) -> np.ndarray:
    """The square around ``face`` (normalised box) in an H×W×3 image."""
    h, w = rgb.shape[:2]
    side = max(face.w * w, face.h * h) * (1 + 2 * margin)
    cx, cy = face.cx * w, face.cy * h
    x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    x1, y1 = int(min(w, cx + side / 2)), int(min(h, cy + side / 2))
    return rgb[y0:max(y0 + 1, y1), x0:max(x0 + 1, x1)]


def grey64(rgb: np.ndarray) -> np.ndarray:
    small = _resize(rgb, INPUT, INPUT).astype(np.float32)
    return small[..., 0] * 0.299 + small[..., 1] * 0.587 + small[..., 2] * 0.114


class EmotionModel:
    def __init__(self, model: Path) -> None:
        try:
            import onnxruntime as ort  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - installed with faster-whisper
            raise FaceError("onnxruntime is not installed.") from exc
        options = ort.SessionOptions()
        options.log_severity_level = 3
        options.intra_op_num_threads = 2
        self._session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name

    def read(self, rgb: np.ndarray, face: Face) -> Emotion:
        """The emotion of ``face`` (a box normalised to ``rgb``)."""
        blob = grey64(face_crop(rgb, face))[None, None].astype(np.float32)  # FER+ takes raw 0..255 grey
        (logits,) = self._session.run(None, {self._input: blob})
        return Emotion(tuple(float(p) for p in softmax(logits)))

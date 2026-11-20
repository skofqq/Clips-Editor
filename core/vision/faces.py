"""Offline face detection with the Ultra-Light-Fast face detector (RFB-320).

The 1.3 MB ONNX model (MIT licence, from the ONNX model zoo) is downloaded
once into ``<data>/models/face`` and runs on the CPU through onnxruntime,
which faster-whisper already installs. A Twitch webcam is a small part of
the frame, so a frame is searched as a whole and in overlapping tiles.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

MODEL_FILE = "version-RFB-320.onnx"
MODEL_URL = ("https://media.githubusercontent.com/media/onnx/models/main/validated/vision/body_analysis/"
             "ultraface/models/version-RFB-320.onnx")
MODEL_SHA256 = "34cd7e60aeff28744c657de7a3dc64e872d506741de66987f3426f2b79f88017"
INPUT_W, INPUT_H = 320, 240
SCORE_THRESHOLD = 0.7
NMS_IOU = 0.3

Progress = Callable[[float, str], None]


class FaceError(RuntimeError):
    pass


class FaceCancelled(Exception):
    pass


@dataclass(frozen=True)
class Face:
    """A face box, normalised to the searched image (0..1)."""

    x1: float
    y1: float
    x2: float
    y2: float
    score: float

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def w(self) -> float:
        return self.x2 - self.x1

    @property
    def h(self) -> float:
        return self.y2 - self.y1


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
        fetch(MODEL_URL, path, 2, "the face model", progress, cancel)
    except DownloadCancelled as exc:
        raise FaceCancelled() from exc
    except DownloadError as exc:
        raise FaceError(f"Could not download the face model: {exc}") from exc
    if _sha256(path) != MODEL_SHA256:
        path.unlink(missing_ok=True)
        raise FaceError("The downloaded face model is damaged. Try again.")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float = NMS_IOU) -> list[int]:
    """Indices kept by greedy non-maximum suppression (boxes: N×4 x1,y1,x2,y2)."""
    order = np.argsort(-scores)
    keep: list[int] = []
    areas = np.clip(boxes[:, 2] - boxes[:, 0], 0, None) * np.clip(boxes[:, 3] - boxes[:, 1], 0, None)
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        xx1 = np.maximum(boxes[i, 0], boxes[rest, 0])
        yy1 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx2 = np.minimum(boxes[i, 2], boxes[rest, 2])
        yy2 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        union = areas[i] + areas[rest] - inter
        order = rest[inter / np.maximum(union, 1e-9) <= iou]
    return keep


def tiles(width: int, height: int) -> list[tuple[float, float, float, float]]:
    """Search windows (normalised x, y, w, h): the whole frame and a 3×3
    grid of half-size overlapping tiles, for faces in a small webcam."""
    result = [(0.0, 0.0, 1.0, 1.0)]
    for row in range(3):
        for col in range(3):
            result.append((col * 0.25, row * 0.25, 0.5, 0.5))
    return result


class FaceDetector:
    def __init__(self, model: Path, threshold: float = SCORE_THRESHOLD) -> None:
        try:
            import onnxruntime as ort  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - installed with faster-whisper
            raise FaceError("onnxruntime is not installed.") from exc
        options = ort.SessionOptions()
        options.log_severity_level = 3
        options.intra_op_num_threads = 2
        self._session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name
        self.threshold = threshold

    def _run(self, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """``rgb``: H×W×3 uint8 → (boxes N×4 normalised, scores N) above the threshold."""
        img = _resize(rgb, INPUT_W, INPUT_H).astype(np.float32)
        img = (img - 127.0) / 128.0
        blob = img.transpose(2, 0, 1)[None]
        scores, boxes = self._session.run(None, {self._input: blob})
        scores = scores[0, :, 1]
        mask = scores > self.threshold
        return boxes[0][mask], scores[mask]

    def detect(self, rgb: np.ndarray, search_tiles: bool = True) -> list[Face]:
        """Faces in an H×W×3 uint8 image, boxes normalised to that image."""
        h, w = rgb.shape[:2]
        all_boxes, all_scores = [], []
        for tx, ty, tw, th in (tiles(w, h) if search_tiles else [(0.0, 0.0, 1.0, 1.0)]):
            x0, y0 = int(tx * w), int(ty * h)
            x1, y1 = int((tx + tw) * w), int((ty + th) * h)
            boxes, scores = self._run(rgb[y0:y1, x0:x1])
            if len(boxes):
                boxes = boxes.copy()
                boxes[:, [0, 2]] = tx + boxes[:, [0, 2]] * tw
                boxes[:, [1, 3]] = ty + boxes[:, [1, 3]] * th
                all_boxes.append(boxes)
                all_scores.append(scores)
        if not all_boxes:
            return []
        boxes = np.clip(np.concatenate(all_boxes), 0.0, 1.0)
        scores = np.concatenate(all_scores)
        return [Face(*map(float, boxes[i]), float(scores[i])) for i in nms(boxes, scores)
                if boxes[i, 2] > boxes[i, 0] and boxes[i, 3] > boxes[i, 1]]


def _resize(rgb: np.ndarray, width: int, height: int) -> np.ndarray:
    """Nearest-neighbour-free bilinear resize in NumPy (no OpenCV needed)."""
    h, w = rgb.shape[:2]
    if (w, h) == (width, height):
        return rgb
    ys = (np.arange(height) + 0.5) * h / height - 0.5
    xs = (np.arange(width) + 0.5) * w / width - 0.5
    y0 = np.clip(np.floor(ys).astype(int), 0, h - 1)
    x0 = np.clip(np.floor(xs).astype(int), 0, w - 1)
    y1, x1 = np.clip(y0 + 1, 0, h - 1), np.clip(x0 + 1, 0, w - 1)
    fy = np.clip(ys - y0, 0, 1)[:, None, None]
    fx = np.clip(xs - x0, 0, 1)[None, :, None]
    src = rgb.astype(np.float32)
    top = src[y0][:, x0] * (1 - fx) + src[y0][:, x1] * fx
    bottom = src[y1][:, x0] * (1 - fx) + src[y1][:, x1] * fx
    return (top * (1 - fy) + bottom * fy).astype(np.uint8)

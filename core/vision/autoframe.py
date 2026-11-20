"""Webcam auto-framing: find the streamer's face and frame the webcam crop
around it, optionally following the face as it moves.

1. Frames are sampled across the kept part of the clip (about 2 per second).
2. A few of them are searched whole and in tiles to find where faces are;
   the face seen most often (preferring one inside the current webcam crop)
   is the streamer.
3. Every sample is then searched only around that face, which is fast.
4. The crop gets the slot's aspect, head-and-shoulders size, the face a bit
   above the centre. With *follow* the crop position is smoothed over time
   and reduced to a few keys; small wobbles are ignored.
"""

from __future__ import annotations

import logging
import math
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Callable, Iterator

import numpy as np

from core.editor.geometry import Rect
from core.editor.track import TrackKey, clean_track
from core.media.ffmpeg_locator import no_window_kwargs
from core.vision.faces import Face, FaceCancelled, FaceDetector

log = logging.getLogger(__name__)

SAMPLE_W, SAMPLE_H = 960, 540  # frames are analysed at this size
SAMPLE_FPS = 2.0
MAX_SAMPLES = 240
FIND_FRAMES = 8  # frames searched in full to find the face
HEAD_ROOM = 2.6  # crop height in face heights (head and shoulders)
FACE_Y = 0.42  # face centre at this share of the crop height
SMOOTH_S = 1.5  # moving-average window of the path
TOLERANCE = 0.03  # path simplification tolerance, share of the crop size
MIN_MOTION = 0.04  # less motion than this (share of the crop) = keep the crop still

Progress = Callable[[float, str], None]


class AutoframeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Sample:
    t_us: int
    face: Face | None  # normalised to the source frame


@dataclass(frozen=True)
class FrameResult:
    crop: Rect  # normalised crop of the source
    track: tuple[TrackKey, ...]  # empty when the face stays put
    found: int  # samples with the face
    total: int


# ------------------------------------------------------------------ frames
def sample_frames(ffmpeg: Path, source: Path, start_us: int, end_us: int, fps: float = SAMPLE_FPS,
                  cancel: threading.Event | None = None) -> Iterator[tuple[int, np.ndarray]]:
    """(source time, RGB frame SAMPLE_W×SAMPLE_H) about ``fps`` times a second."""
    length = max(0.1, (end_us - start_us) / 1_000_000)
    fps = min(fps, MAX_SAMPLES / length)
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-ss", f"{start_us / 1_000_000:.3f}", "-t", f"{length:.3f}", "-i", str(source),
           "-an", "-vf", f"fps={fps:.4f},scale={SAMPLE_W}:{SAMPLE_H}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **no_window_kwargs())
    size = SAMPLE_W * SAMPLE_H * 3
    index = 0
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise FaceCancelled()
            data = process.stdout.read(size)
            if len(data) < size:
                break
            yield start_us + int(index / fps * 1_000_000), np.frombuffer(data, np.uint8).reshape(SAMPLE_H, SAMPLE_W, 3)
            index += 1
    finally:
        process.kill()
        process.wait()


# ------------------------------------------------------------------ search
def _window(face: Face, scale: float = 5.0) -> tuple[float, float, float, float]:
    """Square-ish search window around a face (normalised x, y, w, h)."""
    side_px = max(face.w * SAMPLE_W, face.h * SAMPLE_H) * scale
    side_px = max(side_px, 200.0)
    w, h = min(1.0, side_px / SAMPLE_W), min(1.0, side_px / SAMPLE_H)
    x = min(max(0.0, face.cx - w / 2), 1.0 - w)
    y = min(max(0.0, face.cy - h / 2), 1.0 - h)
    return x, y, w, h


def _detect_in(detector: FaceDetector, frame: np.ndarray, window: tuple[float, float, float, float]) -> list[Face]:
    x, y, w, h = window
    x0, y0 = int(x * SAMPLE_W), int(y * SAMPLE_H)
    x1, y1 = max(x0 + 2, int((x + w) * SAMPLE_W)), max(y0 + 2, int((y + h) * SAMPLE_H))
    faces = detector.detect(frame[y0:y1, x0:x1], search_tiles=False)
    return [Face(x + f.x1 * w, y + f.y1 * h, x + f.x2 * w, y + f.y2 * h, f.score) for f in faces]


def choose_face(found: list[list[Face]], prefer: Rect | None = None) -> Face | None:
    """The face seen in the most frames (a cluster), preferring one inside
    ``prefer`` (the current webcam crop); returns the cluster's median face."""
    clusters: list[list[Face]] = []
    for faces in found:
        for face in faces:
            for cluster in clusters:
                ref = cluster[0]
                if math.hypot(face.cx - ref.cx, (face.cy - ref.cy) * SAMPLE_H / SAMPLE_W) < max(ref.w, face.w) * 1.5:
                    cluster.append(face)
                    break
            else:
                clusters.append([face])
    if not clusters:
        return None

    def score(cluster: list[Face]) -> float:
        size = median(f.h for f in cluster)
        value = len(cluster) * (0.5 + size)
        if prefer is not None and prefer.contains(cluster[0].cx, cluster[0].cy):
            value *= 3
        return value

    best = max(clusters, key=score)
    return Face(median(f.x1 for f in best), median(f.y1 for f in best), median(f.x2 for f in best),
                median(f.y2 for f in best), median(f.score for f in best))


def track_face(frames: list[tuple[int, np.ndarray]], detector: FaceDetector, prefer: Rect | None = None,
               progress: Progress | None = None, cancel: threading.Event | None = None) -> list[Sample]:
    """Find the main face, then follow it through all frames."""
    if not frames:
        return []
    step = max(1, len(frames) // FIND_FRAMES)
    probes = frames[::step][:FIND_FRAMES]
    found = []
    for i, (_t, frame) in enumerate(probes):
        if cancel is not None and cancel.is_set():
            raise FaceCancelled()
        found.append(detector.detect(frame))
        if progress:
            progress(0.3 + 0.2 * (i + 1) / len(probes), "Looking for the face…")
    anchor = choose_face(found, prefer)
    if anchor is None:
        return [Sample(t, None) for t, _ in frames]
    samples: list[Sample] = []
    last = anchor
    for i, (t, frame) in enumerate(frames):
        if cancel is not None and cancel.is_set():
            raise FaceCancelled()
        faces = _detect_in(detector, frame, _window(last))
        if not faces and last is not anchor:
            faces = _detect_in(detector, frame, _window(anchor))  # lost it: look where it usually is
        best = None
        if faces:
            best = min(faces, key=lambda f: math.hypot(f.cx - last.cx, f.cy - last.cy) + abs(f.h - anchor.h))
            if abs(best.h - anchor.h) > anchor.h * 0.8:  # a different face (a picture, a guest)
                best = None
        samples.append(Sample(t, best))
        if best is not None:
            last = best
        if progress and i % 4 == 0:
            progress(0.5 + 0.45 * (i + 1) / len(frames), "Following the face…")
    return samples


# ------------------------------------------------------------------ framing
def _smooth(values: list[float], window: int) -> list[float]:
    if window <= 1 or len(values) < 3:
        return values
    half = window // 2
    padded = [values[0]] * half + values + [values[-1]] * half
    return [sum(padded[i:i + window]) / window for i in range(len(values))]


def _simplify(points: list[tuple[int, float, float]], tol_x: float, tol_y: float) -> list[tuple[int, float, float]]:
    """Ramer–Douglas–Peucker on (t, x, y): keep the keys the motion needs."""
    if len(points) <= 2:
        return points
    t0, x0, y0 = points[0]
    t1, x1, y1 = points[-1]
    worst, index = 0.0, 0
    for i in range(1, len(points) - 1):
        t, x, y = points[i]
        f = (t - t0) / max(1, t1 - t0)
        err = max(abs(x - (x0 + (x1 - x0) * f)) / tol_x, abs(y - (y0 + (y1 - y0) * f)) / tol_y)
        if err > worst:
            worst, index = err, i
    if worst <= 1.0:
        return [points[0], points[-1]]
    return _simplify(points[:index + 1], tol_x, tol_y)[:-1] + _simplify(points[index:], tol_x, tol_y)


def frame_crop(samples: list[Sample], source_w: int, source_h: int, slot_aspect: float,
               follow: bool = True) -> FrameResult | None:
    """Crop (normalised) around the face and, with ``follow``, its track."""
    hits = [s for s in samples if s.face is not None]
    if not hits:
        return None
    face_h_px = median(s.face.h for s in hits) * source_h
    crop_h = min(float(source_h), face_h_px * HEAD_ROOM)
    crop_w = crop_h * slot_aspect
    if crop_w > source_w:
        crop_w = float(source_w)
        crop_h = crop_w / slot_aspect
    cw, ch = crop_w / source_w, crop_h / source_h  # normalised size

    def top_left(face: Face) -> tuple[float, float]:
        x = min(max(0.0, face.cx - cw / 2), 1.0 - cw)
        y = min(max(0.0, face.cy - ch * FACE_Y), 1.0 - ch)
        return x, y

    corners = [top_left(s.face) for s in hits]
    base_x, base_y = median(c[0] for c in corners), median(c[1] for c in corners)
    crop = Rect(base_x, base_y, cw, ch)
    track: tuple[TrackKey, ...] = ()
    if follow and len(hits) >= 3:
        # Fill the gaps (face not found) with the last known position.
        xs, ys, last = [], [], corners[0]
        lookup = {s.t_us: top_left(s.face) for s in hits}
        for s in samples:
            last = lookup.get(s.t_us, last)
            xs.append(last[0])
            ys.append(last[1])
        spacing_s = (samples[-1].t_us - samples[0].t_us) / max(1, len(samples) - 1) / 1_000_000 or 0.5
        window = max(1, int(round(SMOOTH_S / spacing_s)) | 1)
        xs, ys = _smooth(xs, window), _smooth(ys, window)
        if max(max(xs) - min(xs), 0.0) > cw * MIN_MOTION or max(ys) - min(ys) > ch * MIN_MOTION:
            points = _simplify([(s.t_us, x, y) for s, x, y in zip(samples, xs, ys)], cw * TOLERANCE, ch * TOLERANCE)
            track = clean_track(TrackKey(t, x - base_x, y - base_y) for t, x, y in points)
    return FrameResult(crop=crop, track=track, found=len(hits), total=len(samples))


def autoframe(ffmpeg: Path, source: Path, detector_factory: Callable[[], FaceDetector], start_us: int, end_us: int,
              source_w: int, source_h: int, slot_aspect: float, prefer: Rect | None = None, follow: bool = True,
              progress: Progress | None = None, cancel: threading.Event | None = None) -> FrameResult:
    """Whole job (runs in a worker thread)."""
    progress = progress or (lambda f, m: None)
    progress(0.02, "Preparing the face model…")
    detector = detector_factory()
    progress(0.05, "Reading frames…")
    frames = list(sample_frames(ffmpeg, source, start_us, end_us, cancel=cancel))
    if not frames:
        raise AutoframeError("Could not read frames from the video.")
    progress(0.3, "Looking for the face…")
    samples = track_face(frames, detector, prefer, progress, cancel)
    result = frame_crop(samples, source_w, source_h, slot_aspect, follow)
    if result is None:
        raise AutoframeError("No face was found. Make sure the webcam shows a face, or set the crop by hand.")
    progress(1.0, "Done")
    return result

"""Auto cover: find the frame where the streamer's face shows the strongest
emotion, then set up a cover with that face big and a large title.

Frames are sampled over the Short (more around the loudest moments), the
face is searched inside the webcam region of each frame, and every face is
scored by the FER+ emotion model (laughing and shocked faces win), its size
and its sharpness, plus a bonus near the clip's loud peaks. Without the
emotion model the choice falls back to face size, sharpness and loudness.
Runs off the GUI thread.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import numpy as np

from core.editor.geometry import Rect
from core.media.ffmpeg_locator import no_window_kwargs
from core.thumbnails.model import BOTTOM, TOP, ThumbnailDesign
from core.vision.faces import Face

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]
US = 1_000_000
MAX_SAMPLES = 36
MIN_GAP_US = 400_000
GRAB_WIDTH = 720  # the webcam region is read at up to this width
AUTO_TEXT_SIZE = 170


class AutoCoverCancelled(Exception):
    pass


@dataclass(frozen=True)
class CoverPick:
    source_us: int
    score: float
    face: tuple[float, float, float, float] | None = None  # 0..1 box of the source frame
    emotion: str = ""  # e.g. "surprised 82%"
    strength: float = 0.0  # emotion 0..1

    @property
    def note(self) -> str:
        if self.face is None:
            return "no face"
        return self.emotion or "face"


@dataclass(frozen=True)
class AutoCoverResult:
    picks: list[CoverPick]  # best first
    sampled: int
    with_emotions: bool  # False: the emotion model was not available

    @property
    def best(self) -> CoverPick | None:
        return self.picks[0] if self.picks else None


# ------------------------------------------------------------------ when to look

def sample_times(output_us: int, to_source: Callable[[int], int], peaks=(), count: int = 24,
                 limit: int = MAX_SAMPLES) -> list[int]:
    """Source times to look at: evenly over the Short, plus the moments right
    after each loud peak (a reaction comes a beat after the sound)."""
    if output_us <= 0:
        return []
    chosen: list[int] = []

    def add(us: int) -> None:
        if len(chosen) < limit and all(abs(us - c) >= MIN_GAP_US for c in chosen):
            chosen.append(int(us))

    for us in list(peaks)[:4]:
        for delay in (300_000, 900_000, 0):
            add(int(us) + delay)
    for i in range(count):
        add(int(to_source(int(output_us * (i + 0.5) / count))))
    return sorted(chosen)


# ------------------------------------------------------------------ reading frames

def grab_region(ffmpeg: Path, video: Path, source_us: int, region: Rect, src_w: int, src_h: int,
                width: int = GRAB_WIDTH, timeout: float = 30.0) -> np.ndarray | None:
    """H×W×3 RGB of ``region`` (0..1 of the source) at ``source_us``, or None."""
    x, y = int(region.x * src_w), int(region.y * src_h)
    w, h = max(16, int(region.w * src_w)) // 2 * 2, max(16, int(region.h * src_h)) // 2 * 2
    out_w = min(width, w) // 2 * 2
    out_h = max(2, int(round(h * out_w / w / 2)) * 2)
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-ss", f"{max(0, source_us) / US:.3f}", "-i", str(video), "-frames:v", "1",
           "-vf", f"crop={w}:{h}:{x}:{y},scale={out_w}:{out_h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, **no_window_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Auto cover: frame %s failed: %s", source_us, exc)
        return None
    if result.returncode != 0 or len(result.stdout) < out_w * out_h * 3:
        return None
    return np.frombuffer(result.stdout[: out_w * out_h * 3], dtype=np.uint8).reshape(out_h, out_w, 3)


def search_region(webcam: Rect, pad: float = 0.15) -> Rect:
    """The webcam crop with a margin, kept inside the frame."""
    x = max(0.0, webcam.x - webcam.w * pad)
    y = max(0.0, webcam.y - webcam.h * pad)
    w = min(1.0, webcam.x + webcam.w * (1 + pad)) - x
    h = min(1.0, webcam.y + webcam.h * (1 + pad)) - y
    return Rect(x, y, max(0.01, w), max(0.01, h))


def sharpness(rgb: np.ndarray) -> float:
    """Variance of a Laplacian of the grey image (blurry motion frames score low)."""
    grey = rgb.astype(np.float32).mean(axis=2)
    if grey.shape[0] < 3 or grey.shape[1] < 3:
        return 0.0
    lap = (grey[1:-1, :-2] + grey[1:-1, 2:] + grey[:-2, 1:-1] + grey[2:, 1:-1] - 4 * grey[1:-1, 1:-1])
    return float(lap.var())


def main_face(faces: list[Face]) -> Face | None:
    """The streamer: the biggest of the most confident faces (a less sure,
    bigger box is usually a shirt or a poster)."""
    if not faces:
        return None
    best = max(f.score for f in faces)
    return max((f for f in faces if f.score >= best - 0.05), key=lambda f: f.w * f.h)


def detect_face(detector, rgb: np.ndarray) -> Face | None:
    """The webcam picture is already close up: look at it whole first, and
    in tiles only when that finds nothing."""
    try:
        found = detector.detect(rgb, search_tiles=False)
    except TypeError:  # detectors without the option
        found = detector.detect(rgb)
    return main_face(found or detector.detect(rgb))


# ------------------------------------------------------------------ scoring

@dataclass
class _Sample:
    us: int
    face: Face | None = None  # normalised to the searched region
    strength: float = 0.0
    emotion: str = ""
    sharp: float = 0.0
    size: float = 0.0
    near_peak: float = 0.0


def score_samples(samples: list[_Sample], with_emotions: bool) -> list[float]:
    """0..1 per sample. Faces always beat frames without one."""
    sharp_max = max((s.sharp for s in samples if s.face is not None), default=0.0) or 1.0
    size_max = max((s.size for s in samples if s.face is not None), default=0.0) or 1.0
    scores = []
    for s in samples:
        if s.face is None:
            scores.append(0.1 * s.near_peak)
            continue
        sharp, size = min(1.0, s.sharp / sharp_max), min(1.0, s.size / size_max)
        if with_emotions:
            value = 0.6 * s.strength + 0.12 * size + 0.13 * sharp + 0.15 * s.near_peak
        else:
            value = 0.3 * size + 0.35 * sharp + 0.35 * s.near_peak
        scores.append(0.2 + 0.8 * value)
    return scores


def near_peak(us: int, peaks, window_us: int = 2_500_000) -> float:
    """1 right after a loud peak, fading to 0 over ``window_us``."""
    best = 0.0
    for p in peaks:
        d = us - int(p)
        if -500_000 <= d <= window_us:
            best = max(best, 1.0 - max(0, d) / window_us)
    return best


def find_cover_frames(
    ffmpeg: Path,
    video: Path,
    times: list[int],
    webcam: Rect,
    src_w: int,
    src_h: int,
    detector_factory: Callable[[], object],
    emotion_factory: Callable[[], object] | None = None,
    peaks=(),
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    grab: Callable[..., np.ndarray | None] = grab_region,
) -> AutoCoverResult:
    """Look at each time in ``times`` and rank them as covers (best first)."""
    progress = progress or (lambda f, m="": None)
    progress(0.0, "Loading the face model…")
    detector = detector_factory()
    emotions = None
    if emotion_factory is not None:
        progress(0.0, "Loading the emotion model…")
        try:
            emotions = emotion_factory()
        except Exception as exc:  # noqa: BLE001 - the cover still works on face size and loudness
            if type(exc).__name__ == "FaceCancelled":
                raise AutoCoverCancelled() from exc
            log.warning("Auto cover: no emotion model (%s); ranking by face size and loudness", exc)
    region = search_region(webcam)
    samples: list[_Sample] = []
    for index, us in enumerate(times):
        if cancel is not None and cancel.is_set():
            raise AutoCoverCancelled()
        progress(index / max(1, len(times)), f"Looking at frame {index + 1} of {len(times)}…")
        rgb = grab(ffmpeg, video, us, region, src_w, src_h)
        sample = _Sample(int(us), near_peak=near_peak(us, peaks))
        if rgb is not None:
            face = detect_face(detector, rgb)
            if face is not None:
                from core.vision.emotion import face_crop

                crop = face_crop(rgb, face)
                sample.face, sample.size, sample.sharp = face, face.w * face.h, sharpness(crop)
                if emotions is not None:
                    try:
                        emotion = emotions.read(rgb, face)
                        sample.strength, sample.emotion = emotion.strength, emotion.text
                    except Exception as exc:  # noqa: BLE001 - one bad face does not stop the search
                        log.warning("Auto cover: emotion failed at %s: %s", us, exc)
        samples.append(sample)
    progress(1.0, "Choosing…")
    scores = score_samples(samples, emotions is not None)
    picks = []
    for sample, score in sorted(zip(samples, scores), key=lambda p: -p[1]):
        face = None
        if sample.face is not None:  # back to 0..1 of the whole source frame
            f = sample.face
            face = tuple(round(v, 5) for v in (region.x + f.x1 * region.w, region.y + f.y1 * region.h,
                                               region.x + f.x2 * region.w, region.y + f.y2 * region.h))
        picks.append(CoverPick(sample.us, round(score, 4), face, sample.emotion, round(sample.strength, 4)))
    return AutoCoverResult(picks, len(samples), emotions is not None)


# ------------------------------------------------------------------ the design

def apply_pick(design: ThumbnailDesign, pick: CoverPick, face_y: float | None = None) -> ThumbnailDesign:
    """The cover for ``pick``: its frame, the face big over the gameplay and a
    large title over the webcam (``face_y``: the face's height on the Short, 0..1)."""
    position = design.position
    if face_y is not None:
        position = TOP if face_y < 0.5 else BOTTOM
    return replace(
        design, enabled=True, source_us=int(pick.source_us), face=pick.face, face_cutout=pick.face is not None,
        position=position, offset=0, size=max(design.size, AUTO_TEXT_SIZE), uppercase=True, shade=True,
        zoom=1.0,
    ).clean()

"""Follow an object through the clip: the box drawn over it at one moment is
found again in the frames before and after (template matching with
normalised cross-correlation, numpy only), giving a motion path for a blur.

Frames are read as small grey pictures (about 8 per second). The template is
refreshed slowly while the match is good, so turning or lighting changes do
not lose the object; when the match drops too low the box stays where it was
last seen until the object is found again.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Callable

import numpy as np

from core.editor.geometry import Rect
from core.editor.track import MAX_KEYS, TrackKey, clean_track
from core.media.ffmpeg_locator import no_window_kwargs

TRACK_FPS = 8.0
ANALYSIS_W = 640
MIN_SCORE = 0.35  # below this the object is considered lost
UPDATE_SCORE = 0.6  # above this the template is refreshed
UPDATE_RATE = 0.15
SEARCH = 0.6  # search margin, share of the box size (plus a minimum)
MIN_SEARCH_PX = 16
SIMPLIFY = 0.004  # path simplification tolerance, share of the frame

Progress = Callable[[float, str], None]


class TrackCancelled(Exception):
    pass


class TrackError(RuntimeError):
    pass


def read_frames(ffmpeg: Path, source: Path, start_us: int, end_us: int, src_w: int, src_h: int,
                fps: float = TRACK_FPS, cancel: threading.Event | None = None) -> list[tuple[int, np.ndarray]]:
    """(source time, grey frame) pairs between ``start_us`` and ``end_us``."""
    width = min(ANALYSIS_W, src_w) // 2 * 2
    height = max(2, int(round(src_h * width / src_w / 2)) * 2)
    length = max(0.05, (end_us - start_us) / 1_000_000)
    fps = min(fps, MAX_KEYS / length) if length > 0 else fps
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-ss", f"{start_us / 1_000_000:.3f}", "-t", f"{length:.3f}", "-i", str(source),
           "-an", "-vf", f"fps={fps:.4f},scale={width}:{height}", "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **no_window_kwargs())
    size = width * height
    frames: list[tuple[int, np.ndarray]] = []
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise TrackCancelled()
            data = process.stdout.read(size)
            if len(data) < size:
                break
            t_us = start_us + int(round(len(frames) / fps * 1_000_000))
            frames.append((t_us, np.frombuffer(data, np.uint8).reshape(height, width).astype(np.float32)))
    finally:
        process.stdout.close()
        process.kill()
        process.wait()
    return frames


def match(search: np.ndarray, template: np.ndarray) -> tuple[int, int, float]:
    """Best place (x, y, score) of ``template`` inside ``search`` by normalised
    cross-correlation (score -1..1), computed with FFTs."""
    sh, sw = search.shape
    th, tw = template.shape
    if th > sh or tw > sw:
        return 0, 0, -1.0
    t0 = template - template.mean()
    t_norm = float(np.sqrt((t0 ** 2).sum()))
    if t_norm < 1e-6:
        return (sw - tw) // 2, (sh - th) // 2, 0.0  # flat template: nothing to follow
    fh, fw = sh + th, sw + tw
    corr = np.fft.irfft2(np.fft.rfft2(search, (fh, fw)) * np.conj(np.fft.rfft2(t0, (fh, fw))), (fh, fw))
    corr = corr[: sh - th + 1, : sw - tw + 1]
    # Window sums of the search area and its square (integral images).
    integral = np.pad(search, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    integral2 = np.pad(search ** 2, ((1, 0), (1, 0))).cumsum(0).cumsum(1)

    def window(ii: np.ndarray) -> np.ndarray:
        return ii[th:, tw:] - ii[:-th, tw:] - ii[th:, :-tw] + ii[:-th, :-tw]

    n = th * tw
    s1, s2 = window(integral), window(integral2)
    variance = np.maximum(s2 - s1 ** 2 / n, 0.0)
    score = corr / (np.sqrt(variance) * t_norm + 1e-3)
    y, x = np.unravel_index(int(np.argmax(score)), score.shape)
    return int(x), int(y), float(score[y, x])


def follow(frames: list[tuple[int, np.ndarray]], box: tuple[float, float, float, float],
           cancel: threading.Event | None = None,
           progress: Callable[[int], None] | None = None) -> list[tuple[int, float, float, float]]:
    """Track the box (x, y, w, h in frame pixels, as in ``frames[0]``) through
    ``frames``; returns (t_us, x, y, score) per frame."""
    if not frames:
        return []
    h_img, w_img = frames[0][1].shape
    x, y, w, h = box
    w, h = max(4, int(round(w))), max(4, int(round(h)))
    x = min(max(0.0, x), w_img - w)
    y = min(max(0.0, y), h_img - h)
    xi, yi = int(round(x)), int(round(y))
    template = frames[0][1][yi:yi + h, xi:xi + w].copy()
    result = [(frames[0][0], float(xi), float(yi), 1.0)]
    vx = vy = 0.0
    for i, (t_us, frame) in enumerate(frames[1:], 1):
        if cancel is not None and cancel.is_set():
            raise TrackCancelled()
        mx = max(MIN_SEARCH_PX, int(w * SEARCH))
        my = max(MIN_SEARCH_PX, int(h * SEARCH))
        px, py = xi + vx, yi + vy  # predicted from the last motion
        x0 = int(max(0, min(w_img - w, px) - mx))
        y0 = int(max(0, min(h_img - h, py) - my))
        x1 = int(min(w_img, max(0, px) + w + mx))
        y1 = int(min(h_img, max(0, py) + h + my))
        bx, by, score = match(frame[y0:y1, x0:x1], template)
        if score >= MIN_SCORE:
            nx, ny = x0 + bx, y0 + by
            vx, vy = 0.5 * vx + 0.5 * (nx - xi), 0.5 * vy + 0.5 * (ny - yi)
            xi, yi = nx, ny
            if score >= UPDATE_SCORE:
                patch = frame[yi:yi + h, xi:xi + w]
                if patch.shape == template.shape:
                    template = (1 - UPDATE_RATE) * template + UPDATE_RATE * patch
        else:
            vx = vy = 0.0  # lost: wait where it was last seen
        result.append((t_us, float(xi), float(yi), score))
        if progress is not None:
            progress(i)
    return result


def _simplify(points: list[tuple[int, float, float]], tol: float) -> list[tuple[int, float, float]]:
    """Ramer–Douglas–Peucker on (t, x, y): drop keys that a straight line covers."""
    if len(points) < 3:
        return points
    (t0, x0, y0), (t1, x1, y1) = points[0], points[-1]
    worst, index = -1.0, 0
    for i in range(1, len(points) - 1):
        t, x, y = points[i]
        f = (t - t0) / max(1, t1 - t0)
        err = max(abs(x - (x0 + (x1 - x0) * f)), abs(y - (y0 + (y1 - y0) * f)))
        if err > worst:
            worst, index = err, i
    if worst <= tol:
        return [points[0], points[-1]]
    return _simplify(points[: index + 1], tol)[:-1] + _simplify(points[index:], tol)


def track_object(ffmpeg: Path, source: Path, src_w: int, src_h: int, rect: Rect, at_us: int,
                 start_us: int, end_us: int, base: Rect, cancel: threading.Event | None = None,
                 progress: Progress | None = None) -> tuple[TrackKey, ...]:
    """Motion keys for a blur whose box is at ``rect`` (normalised) at ``at_us``,
    covering ``start_us``–``end_us``. Keys are offsets from ``base`` (the blur's
    own rectangle)."""
    at_us = min(max(at_us, start_us), end_us)
    report = progress or (lambda _f, _m: None)
    report(0.0, "Reading frames…")
    after = read_frames(ffmpeg, source, at_us, end_us, src_w, src_h, cancel=cancel)
    before = read_frames(ffmpeg, source, start_us, at_us, src_w, src_h, cancel=cancel) if at_us > start_us else []
    if not after and not before:
        raise TrackError("No frames could be read from the video.")
    frames_ref = after or before
    h_img, w_img = frames_ref[0][1].shape
    box = (rect.x * w_img, rect.y * h_img, rect.w * w_img, rect.h * h_img)
    total = max(1, len(after) + len(before))
    done = 0

    def tick(_i: int) -> None:
        nonlocal done
        done += 1
        report(min(0.99, done / total), f"Following the object… {int(done / total * 100)}%")

    path: list[tuple[int, float, float, float]] = []
    if after:
        path += follow(after, box, cancel, tick)
    if before:
        # Backwards from the moment the box was drawn: start at the drawn frame.
        backward = [(at_us, after[0][1])] + list(reversed(before)) if after else list(reversed(before))
        path += follow(backward, box, cancel, tick)[1:]
    path.sort()
    points = [(t, x / w_img, y / h_img) for t, x, y, _score in path]
    points = _simplify(points, SIMPLIFY)
    keys = [TrackKey(t, round(x - base.x, 6), round(y - base.y, 6)) for t, x, y in points]
    report(1.0, "Done")
    return clean_track(keys)  # empty when the object did not move

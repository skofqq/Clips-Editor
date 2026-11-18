"""Small preview images for the media list.

One frame from a quarter of the way into the video, scaled to ``width``
pixels wide, cached by path, size and modification time so a second import
of the same file is instant.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

from core.media.ffmpeg_locator import no_window_kwargs

log = logging.getLogger(__name__)

THUMB_WIDTH = 192


def thumbnail_path(video: Path, cache_dir: Path, width: int = THUMB_WIDTH) -> Path:
    video = Path(video)
    try:
        stat = video.stat()
        stamp = f"{stat.st_size}-{int(stat.st_mtime)}"
    except OSError:
        stamp = "missing"
    key = hashlib.sha1(f"{video.resolve()}|{stamp}|{width}".encode("utf-8")).hexdigest()[:20]
    return Path(cache_dir) / "thumbnails" / f"{key}.jpg"


def make_thumbnail(ffmpeg: Path, video: Path, duration_s: float, cache_dir: Path,
                   width: int = THUMB_WIDTH, timeout: float = 30.0) -> Path | None:
    """Path to a JPEG thumbnail of ``video`` (created if needed), or None."""
    out = thumbnail_path(video, cache_dir, width)
    if out.is_file() and out.stat().st_size > 0:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    seek = max(0.0, min(duration_s * 0.25, max(0.0, duration_s - 0.1)))
    tmp = out.with_suffix(".part.jpg")
    cmd = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-ss", f"{seek:.3f}", "-i", str(video),
        "-frames:v", "1", "-vf", f"scale={int(width)}:-2", "-q:v", "4", str(tmp),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **no_window_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Thumbnail for %s failed: %s", video, exc)
        return None
    if result.returncode != 0 or not tmp.is_file():
        log.info("Thumbnail for %s failed: %s", video, result.stderr.strip()[-300:])
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(out)
    return out

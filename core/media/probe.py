"""Media inspection with FFprobe.

``parse_probe_output`` is pure and unit-tested with stored JSON;
``probe_media`` runs ffprobe. Source files are only ever read.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from core.media.errors import ProbeError
from core.media.ffmpeg_locator import no_window_kwargs

SUPPORTED_EXTENSIONS = (
    ".mp4", ".mkv", ".mov", ".webm", ".flv", ".ts", ".m4v", ".avi",
)


@dataclass(frozen=True)
class VideoStream:
    index: int
    codec: str
    width: int
    height: int
    fps: Fraction
    avg_fps: Fraction
    time_base: Fraction
    pix_fmt: str
    rotation: int
    frame_count: int | None

    @property
    def is_vfr_suspected(self) -> bool:
        """True when the nominal and average frame rates differ noticeably."""
        if not self.fps or not self.avg_fps:
            return False
        return abs(float(self.fps) - float(self.avg_fps)) / float(self.fps) > 0.01

    @property
    def display_size(self) -> tuple[int, int]:
        """Width and height after applying rotation metadata."""
        if self.rotation % 180 == 90:
            return self.height, self.width
        return self.width, self.height


@dataclass(frozen=True)
class AudioStream:
    index: int
    codec: str
    sample_rate: int
    channels: int
    channel_layout: str
    language: str


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    size_bytes: int
    duration_s: float
    container: str
    bit_rate: int | None
    video: VideoStream | None
    audio: tuple[AudioStream, ...] = field(default_factory=tuple)

    @property
    def has_video(self) -> bool:
        return self.video is not None

    @property
    def has_audio(self) -> bool:
        return bool(self.audio)


def _fraction(value: Any) -> Fraction:
    """Parse ffprobe rates like ``"30000/1001"``; zero when unusable."""
    if not value or not isinstance(value, str):
        return Fraction(0)
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            if int(den) == 0:
                return Fraction(0)
            return Fraction(int(num), int(den))
        return Fraction(value)
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def _int(value: Any, default: int | None = 0) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _rotation(stream: dict) -> int:
    rotate = _int((stream.get("tags") or {}).get("rotate"), None)
    if rotate is None:
        for side in stream.get("side_data_list") or []:
            if "rotation" in side:
                rotate = _int(side.get("rotation"), 0)
                break
    # ffprobe reports display-matrix rotation as e.g. -90; normalise to 0..359.
    return (rotate or 0) % 360


def _is_attached_picture(stream: dict) -> bool:
    return bool((stream.get("disposition") or {}).get("attached_pic"))


def parse_probe_output(data: dict, path: Path, size_bytes: int | None = None) -> MediaInfo:
    """Turn ffprobe ``-show_format -show_streams`` JSON into :class:`MediaInfo`."""
    if not isinstance(data, dict):
        raise ProbeError("FFprobe returned unexpected data.")
    fmt = data.get("format") or {}
    streams = data.get("streams") or []

    video: VideoStream | None = None
    audio: list[AudioStream] = []
    stream_durations: list[float] = []

    for stream in streams:
        kind = stream.get("codec_type")
        duration = _float(stream.get("duration"))
        if kind == "video" and video is None and not _is_attached_picture(stream):
            video = VideoStream(
                index=_int(stream.get("index")) or 0,
                codec=stream.get("codec_name", "unknown"),
                width=_int(stream.get("width")) or 0,
                height=_int(stream.get("height")) or 0,
                fps=_fraction(stream.get("r_frame_rate")),
                avg_fps=_fraction(stream.get("avg_frame_rate")),
                time_base=_fraction(stream.get("time_base")),
                pix_fmt=stream.get("pix_fmt", ""),
                rotation=_rotation(stream),
                frame_count=_int(stream.get("nb_frames"), None),
            )
            if duration:
                stream_durations.append(duration)
        elif kind == "audio":
            audio.append(
                AudioStream(
                    index=_int(stream.get("index")) or 0,
                    codec=stream.get("codec_name", "unknown"),
                    sample_rate=_int(stream.get("sample_rate")) or 0,
                    channels=_int(stream.get("channels")) or 0,
                    channel_layout=stream.get("channel_layout", ""),
                    language=(stream.get("tags") or {}).get("language", ""),
                )
            )
            if duration:
                stream_durations.append(duration)

    if video is None and not audio:
        raise ProbeError("The file contains no video or audio streams.")

    duration_s = _float(fmt.get("duration")) or (max(stream_durations) if stream_durations else 0.0)
    if size_bytes is None:
        size_bytes = _int(fmt.get("size")) or 0

    return MediaInfo(
        path=path,
        size_bytes=size_bytes,
        duration_s=duration_s,
        container=fmt.get("format_name", ""),
        bit_rate=_int(fmt.get("bit_rate"), None),
        video=video,
        audio=tuple(audio),
    )


def probe_media(path: str | Path, ffprobe: str | Path, timeout: float = 30.0) -> MediaInfo:
    """Run ffprobe on ``path`` and return its description."""
    path = Path(path)
    if not path.is_file():
        raise ProbeError(f"File not found: {path}")

    cmd = [
        str(ffprobe), "-v", "error", "-hide_banner",
        "-print_format", "json", "-show_format", "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **no_window_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"FFprobe timed out on {path.name}.") from exc
    except OSError as exc:
        raise ProbeError(f"Could not start FFprobe: {exc}") from exc

    if result.returncode != 0:
        message = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        raise ProbeError(f"FFprobe could not read {path.name}: {message}")

    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise ProbeError("FFprobe returned invalid JSON.") from exc

    return parse_probe_output(data, path, size_bytes=path.stat().st_size)


def format_duration(seconds: float) -> str:
    """``HH:MM:SS.mmm`` (hours omitted when zero)."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{ms:03d}"
    return f"{minutes:02d}:{secs:02d}.{ms:03d}"


def format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_fps(fps: Fraction) -> str:
    if not fps:
        return "?"
    value = float(fps)
    return f"{value:.3f}".rstrip("0").rstrip(".")

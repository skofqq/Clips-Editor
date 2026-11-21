"""Export settings and presets."""

from __future__ import annotations

import re

from dataclasses import asdict, dataclass, replace
from typing import Any

H264, HEVC = "h264", "hevc"
VIDEO_CODECS = (H264, HEVC)
MP4, MKV = "mp4", "mkv"
CONTAINERS = (MP4, MKV)
AAC, OPUS = "aac", "opus"
AUDIO_CODECS = (AAC, OPUS)
FPS_CHOICES = (24, 25, 30, 50, 60)
SOURCE_FPS = 0  # keep the source frame rate (capped at 60)
AUTO = "auto"  # encoder: best available, GPU first
CPU = "cpu"  # encoder: always software
QUALITY, BITRATE = "quality", "bitrate"
AUDIO_SAMPLE_RATE = 48_000

# Resolutions offered in the dialog: (label, width, height).
RESOLUTIONS = (
    ("1080 × 1920 (9:16)", 1080, 1920),
    ("720 × 1280 (9:16)", 720, 1280),
    ("1440 × 2560 (9:16)", 1440, 2560),
    ("1080 × 1080 (1:1)", 1080, 1080),
    ("1920 × 1080 (16:9)", 1920, 1080),
)


PROGRESS_TOP, PROGRESS_BOTTOM = "top", "bottom"
PROGRESS_PLACES = (PROGRESS_TOP, PROGRESS_BOTTOM)
PROGRESS_COLORS = (("Twitch purple", "#9146ff"), ("White", "#ffffff"), ("Red", "#ff3040"), ("Yellow", "#ffe600"))
_HEX = re.compile(r"#[0-9a-fA-F]{6}")


def progress_bar_height(canvas_h: int) -> int:
    """Thickness of the progress line: 12 px on a 1920 px tall Short."""
    return max(4, round(canvas_h / 160) // 2 * 2)


@dataclass(frozen=True)
class RenderSettings:
    width: int = 1080
    height: int = 1920
    fps: int = SOURCE_FPS
    video_codec: str = H264
    container: str = MP4
    audio_codec: str = AAC
    audio_bitrate_k: int = 192
    rate_control: str = QUALITY  # quality (CRF/CQ) or bitrate
    quality: int = 20  # CRF / CQ: lower = better and bigger
    video_bitrate_k: int = 12_000  # used when rate_control == bitrate
    encoder: str = AUTO  # auto, cpu, or an FFmpeg encoder name (h264_nvenc, ...)
    beep_hz: int = 1000
    loudness_lufs: float = -14.0  # loudness normalisation target; 0 = off
    progress_bar: str = ""  # "" = none, "top" or "bottom": a line that fills up as the Short plays
    progress_color: str = "#9146ff"

    def cleaned(self) -> "RenderSettings":
        even = lambda v: max(2, int(v) // 2 * 2)  # noqa: E731 - yuv420p needs even sizes
        return replace(
            self,
            width=min(4096, even(self.width)),
            height=min(4096, even(self.height)),
            fps=self.fps if self.fps in FPS_CHOICES else SOURCE_FPS,
            video_codec=self.video_codec if self.video_codec in VIDEO_CODECS else H264,
            container=self.container if self.container in CONTAINERS else MP4,
            audio_codec=self.audio_codec if self.audio_codec in AUDIO_CODECS else AAC,
            audio_bitrate_k=max(64, min(512, int(self.audio_bitrate_k))),
            rate_control=self.rate_control if self.rate_control in (QUALITY, BITRATE) else QUALITY,
            quality=max(0, min(51, int(self.quality))),
            video_bitrate_k=max(500, min(200_000, int(self.video_bitrate_k))),
            encoder=str(self.encoder or AUTO),
            beep_hz=max(200, min(4000, int(self.beep_hz))),
            loudness_lufs=0.0 if not self.loudness_lufs else max(-31.0, min(-5.0, float(self.loudness_lufs))),
            progress_bar=self.progress_bar if self.progress_bar in PROGRESS_PLACES else "",
            progress_color=self.progress_color if _HEX.fullmatch(str(self.progress_color or "")) else "#9146ff",
        )

    @property
    def extension(self) -> str:
        return "." + self.container

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "RenderSettings":
        if not isinstance(data, dict):
            return cls()
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        try:
            return cls(**known).cleaned()
        except (TypeError, ValueError):
            return cls()


PRESETS: dict[str, RenderSettings] = {
    "YouTube Shorts · H.264 MP4": RenderSettings(video_codec=H264, container=MP4, audio_codec=AAC, quality=20),
    "YouTube Shorts · HEVC MP4": RenderSettings(video_codec=HEVC, container=MP4, audio_codec=AAC, quality=23),
    "High Quality · H.265 MKV": RenderSettings(
        video_codec=HEVC, container=MKV, audio_codec=OPUS, quality=18, audio_bitrate_k=256
    ),
    "Standard · H.264 MP4": RenderSettings(video_codec=H264, container=MP4, audio_codec=AAC, quality=23),
}
DEFAULT_PRESET = "YouTube Shorts · H.264 MP4"
CUSTOM_PRESET = "Custom"


def preset_name(settings: RenderSettings) -> str:
    """Name of the preset ``settings`` match (ignoring resolution/fps/encoder), or Custom."""
    for name, preset in PRESETS.items():
        same = replace(settings, width=preset.width, height=preset.height, fps=preset.fps,
                       encoder=preset.encoder, beep_hz=preset.beep_hz,
                       loudness_lufs=preset.loudness_lufs, progress_bar=preset.progress_bar,
                       progress_color=preset.progress_color)
        if same == preset:
            return name
    return CUSTOM_PRESET

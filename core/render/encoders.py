"""Video encoder detection and per-encoder FFmpeg arguments."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.media.ffmpeg_locator import no_window_kwargs
from core.render.settings import AUTO, BITRATE, CPU, H264, HEVC, RenderSettings

log = logging.getLogger(__name__)

NVENC, QSV, AMF, SOFTWARE = "nvenc", "qsv", "amf", "cpu"

# Preference order per codec: GPU first, software last.
CANDIDATES = {
    H264: ("h264_nvenc", "h264_qsv", "h264_amf", "libx264"),
    HEVC: ("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265"),
}
SOFTWARE_ENCODER = {H264: "libx264", HEVC: "libx265"}
LABELS = {NVENC: "NVIDIA NVENC", QSV: "Intel Quick Sync", AMF: "AMD AMF", SOFTWARE: "CPU"}


@dataclass(frozen=True)
class EncoderInfo:
    name: str  # FFmpeg encoder name
    codec: str  # h264 | hevc
    kind: str  # nvenc | qsv | amf | cpu

    @property
    def is_hardware(self) -> bool:
        return self.kind != SOFTWARE

    @property
    def label(self) -> str:
        codec = "H.264" if self.codec == H264 else "H.265"
        return f"{LABELS[self.kind]} {codec} ({self.name})"


def encoder_info(name: str) -> EncoderInfo:
    codec = HEVC if name.startswith(("hevc", "libx265")) else H264
    for kind in (NVENC, QSV, AMF):
        if name.endswith("_" + kind):
            return EncoderInfo(name, codec, kind)
    return EncoderInfo(name, codec, SOFTWARE)


def listed_encoders(ffmpeg: Path, timeout: float = 20.0) -> set[str]:
    """Names from ``ffmpeg -encoders`` (compiled in; not necessarily usable)."""
    try:
        out = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=timeout, **no_window_kwargs(),
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not list encoders: %s", exc)
        return set()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def encoder_works(ffmpeg: Path, name: str, timeout: float = 20.0) -> bool:
    """Encode a few frames: a hardware encoder is listed even without the hardware."""
    cmd = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=0.2",
        "-frames:v", "3", "-pix_fmt", "yuv420p", "-c:v", name, "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **no_window_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Encoder %s test failed: %s", name, exc)
        return False
    if result.returncode != 0:
        log.info("Encoder %s not usable: %s", name, result.stderr.strip()[-300:])
    return result.returncode == 0


def detect_encoders(ffmpeg: Path) -> list[EncoderInfo]:
    """Usable H.264/H.265 encoders, best first within each codec."""
    listed = listed_encoders(ffmpeg)
    found: list[EncoderInfo] = []
    for codec, names in CANDIDATES.items():
        for name in names:
            if name in listed and encoder_works(ffmpeg, name):
                found.append(encoder_info(name))
    log.info("Usable encoders: %s", ", ".join(e.name for e in found) or "none")
    return found


def choose_encoder(settings: RenderSettings, available: list[EncoderInfo] | None) -> str:
    """Resolve ``settings.encoder`` (auto / cpu / name) to an FFmpeg encoder name."""
    codec = settings.video_codec
    usable = [e for e in (available or []) if e.codec == codec]
    if settings.encoder == CPU:
        return SOFTWARE_ENCODER[codec]
    if settings.encoder not in (AUTO, ""):
        info = encoder_info(settings.encoder)
        if info.codec == codec and (available is None or any(e.name == info.name for e in usable)):
            return info.name
    for name in CANDIDATES[codec]:
        if any(e.name == name for e in usable):
            return name
    return SOFTWARE_ENCODER[codec]


def video_args(encoder: str, settings: RenderSettings, fps: float) -> list[str]:
    """Encoder options for quality (CRF/CQ) or bitrate mode."""
    info = encoder_info(encoder)
    q = str(settings.quality)
    rate = f"{settings.video_bitrate_k}k"
    maxrate = f"{int(settings.video_bitrate_k * 1.5)}k"
    bufsize = f"{settings.video_bitrate_k * 2}k"
    by_bitrate = settings.rate_control == BITRATE
    gop = str(max(1, round(fps * 2)))  # a keyframe every 2 s
    args = ["-c:v", encoder, "-g", gop]
    if info.kind == NVENC:
        args += ["-preset", "p5", "-tune", "hq", "-rc", "vbr"]
        args += ["-b:v", rate, "-maxrate", maxrate, "-bufsize", bufsize] if by_bitrate else ["-cq", q, "-b:v", "0"]
        args += ["-bf", "2"] if info.codec == H264 else []
    elif info.kind == QSV:
        args += ["-preset", "medium"]
        args += ["-b:v", rate, "-maxrate", maxrate] if by_bitrate else ["-global_quality", q]
    elif info.kind == AMF:
        args += ["-quality", "quality"]
        if by_bitrate:
            args += ["-rc", "vbr_peak", "-b:v", rate, "-maxrate", maxrate]
        else:
            args += ["-rc", "cqp", "-qp_i", q, "-qp_p", q] + (["-qp_b", q] if info.codec == H264 else [])
    else:
        args += ["-preset", "medium"]
        args += ["-b:v", rate, "-maxrate", maxrate, "-bufsize", bufsize] if by_bitrate else ["-crf", q]
        if encoder == "libx265":
            args += ["-x265-params", "log-level=error"]
    if info.codec == H264:
        args += ["-profile:v", "high"]
    args += ["-pix_fmt", "nv12" if info.kind == QSV else "yuv420p"]
    return args

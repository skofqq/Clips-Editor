"""Grab one full-size frame of the source with FFmpeg (run it off the GUI thread)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from core.media.errors import MediaError
from core.media.ffmpeg_locator import no_window_kwargs


def grab_frame(ffmpeg: Path, video: Path, source_us: int, timeout: float = 30.0) -> bytes:
    """PNG data of the frame at ``source_us``."""
    cmd = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-ss", f"{max(0, source_us) / 1e6:.3f}", "-i", str(video),
        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, **no_window_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaError(f"Could not read the frame: {exc}") from exc
    if result.returncode != 0 or not result.stdout:
        raise MediaError("Could not read the frame: " + result.stderr.decode("utf-8", "replace").strip()[-300:])
    return result.stdout

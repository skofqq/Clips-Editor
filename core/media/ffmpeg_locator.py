"""Finds the ffmpeg and ffprobe executables.

Search order: explicit folder from settings, the bundled ``tools/ffmpeg``
folder next to the application, then the system PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from core.media.errors import FFmpegNotFoundError

_EXE = ".exe" if sys.platform == "win32" else ""


@dataclass(frozen=True)
class FFmpegPaths:
    ffmpeg: Path
    ffprobe: Path


def bundled_dir() -> Path:
    from app.config import project_root

    return project_root() / "tools" / "ffmpeg"


def _pair_in(folder: Path) -> FFmpegPaths | None:
    """Look for both tools in ``folder`` or ``folder/bin``."""
    for candidate in (folder, folder / "bin"):
        ffmpeg = candidate / f"ffmpeg{_EXE}"
        ffprobe = candidate / f"ffprobe{_EXE}"
        if ffmpeg.is_file() and ffprobe.is_file():
            return FFmpegPaths(ffmpeg, ffprobe)
    return None


def locate_ffmpeg(
    configured_dir: str | Path | None = None,
    *,
    bundled: Path | None = None,
    search_path: bool = True,
) -> FFmpegPaths:
    """Return the tool paths or raise :class:`FFmpegNotFoundError`."""
    folders: list[Path] = []
    if configured_dir:
        folders.append(Path(configured_dir))
    folders.append(bundled if bundled is not None else bundled_dir())

    for folder in folders:
        found = _pair_in(folder)
        if found:
            return found

    if search_path:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if ffmpeg and ffprobe:
            return FFmpegPaths(Path(ffmpeg), Path(ffprobe))

    raise FFmpegNotFoundError(
        "FFmpeg and FFprobe were not found. Run 'python tools/setup_ffmpeg.py' "
        "or install FFmpeg and add it to PATH."
    )


def no_window_kwargs() -> dict:
    """Keyword arguments that stop a console window flashing on Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def tool_version(executable: Path, timeout: float = 10.0) -> str:
    """First line of ``<tool> -version`` or an empty string on failure."""
    try:
        result = subprocess.run(
            [str(executable), "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = result.stdout.splitlines()
    return lines[0].strip() if lines else ""

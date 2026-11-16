from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.media.errors import FFmpegNotFoundError  # noqa: E402
from core.media.ffmpeg_locator import FFmpegPaths, locate_ffmpeg  # noqa: E402


@pytest.fixture(scope="session")
def ffmpeg_paths() -> FFmpegPaths:
    try:
        return locate_ffmpeg()
    except FFmpegNotFoundError:
        pytest.skip("FFmpeg is not installed")


@pytest.fixture(scope="session")
def sample_clips(tmp_path_factory, ffmpeg_paths: FFmpegPaths) -> dict[str, Path]:
    """Small synthetic clips that mimic Twitch sources (generated once)."""
    folder = tmp_path_factory.mktemp("clips")
    clips = {
        "mp4": (folder / "stream clip.mp4", [
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        ]),
        "mkv_noaudio": (folder / "silent.mkv", [
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=60:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
        ]),
    }
    result = {}
    for key, (path, args) in clips.items():
        subprocess.run(
            [str(ffmpeg_paths.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", *args, str(path)],
            check=True,
        )
        result[key] = path
    return result


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "appdata"
    monkeypatch.setenv("TSS_DATA_DIR", str(folder))
    return folder


def has_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

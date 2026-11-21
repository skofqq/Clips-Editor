"""``TwitchShortsStudio.exe --self-test report.txt``: checks a packaged build
without opening a window (the exe has no console, so the result goes to a file).

Imports every heavy library the app loads later, finds FFmpeg, loads the
theme and draws one icon. Exit code 0 means the build is complete.
"""

from __future__ import annotations

import importlib
import os
import traceback
from pathlib import Path

MODULES = (
    "PySide6.QtWidgets", "PySide6.QtMultimedia", "PySide6.QtSvg", "numpy", "keyring", "yt_dlp",
    "faster_whisper", "ctranslate2", "onnxruntime", "av",
    "app.bootstrap", "ui.main_window.main_window", "core.render.job", "core.projects.clip_memory",
)


def run_self_test(report: str = "") -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    lines: list[str] = []
    ok = True
    for name in MODULES:
        try:
            importlib.import_module(name)
            lines.append(f"ok   import {name}")
        except Exception as exc:  # noqa: BLE001 - every failure is reported
            ok = False
            lines.append(f"FAIL import {name}: {exc!r}")
    try:
        from app.config import APP_VERSION, project_root
        from core.media.ffmpeg_locator import locate_ffmpeg

        paths = locate_ffmpeg()
        lines.append(f"ok   ffmpeg {paths.ffmpeg}")
        lines.append(f"ok   version {APP_VERSION}, root {project_root()}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(f"FAIL ffmpeg: {exc!r}")
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        from ui.theme import icon, stylesheet

        assert len(stylesheet()) > 1000, "stylesheet is empty"
        assert not icon("render").isNull() and not icon("logo_youtube").isNull(), "icons are missing"
        for guide in ("docs/YOUTUBE_SETUP_RU.md", "docs/TIKTOK_SETUP_RU.md", "docs/INSTAGRAM_SETUP_RU.md"):
            assert (project_root() / guide).is_file(), f"{guide} is missing"
        lines.append("ok   theme, icons and guides")
        del app
    except Exception:  # noqa: BLE001
        ok = False
        lines.append("FAIL theme: " + traceback.format_exc())
    lines.append("RESULT " + ("OK" if ok else "FAILED"))
    text = "\n".join(lines) + "\n"
    if report:
        Path(report).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0 if ok else 1

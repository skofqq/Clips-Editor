"""Creates the QApplication, applies the theme and opens the main window."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.config import APP_NAME, APP_VERSION, AppConfig
from app.logging_setup import setup_logging
from core.media.errors import FFmpegNotFoundError
from core.media.ffmpeg_locator import FFmpegPaths, locate_ffmpeg

log = logging.getLogger(__name__)

THEME_FILE = Path(__file__).resolve().parent.parent / "ui" / "theme" / "dark.qss"


def load_theme() -> str:
    try:
        text = THEME_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Theme not loaded: %s", exc)
        return ""
    # Qt wants forward slashes in url(); the folder may contain spaces, so urls are quoted.
    return text.replace("@ICONS@", (THEME_FILE.parent / "icons").as_posix())


def create_application(argv: list[str]) -> QApplication:
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    app.setStyleSheet(load_theme())
    return app


def find_ffmpeg(config: AppConfig) -> FFmpegPaths | None:
    try:
        paths = locate_ffmpeg(config.ffmpeg_dir or None)
    except FFmpegNotFoundError as exc:
        log.warning("%s", exc)
        return None
    log.info("FFmpeg: %s | FFprobe: %s", paths.ffmpeg, paths.ffprobe)
    return paths


def run(argv: list[str]) -> int:
    config = AppConfig.load()
    log_file = setup_logging(config.log_dir)
    log.info("Starting %s %s (log: %s)", APP_NAME, APP_VERSION, log_file)

    app = create_application(argv)

    # Imported here so that logging is ready before any UI module loads.
    from ui.main_window.main_window import MainWindow

    window = MainWindow(config=config, ffmpeg=find_ffmpeg(config))
    window.show()

    files = [arg for arg in argv[1:] if not arg.startswith("-")]
    if files:
        window.import_files(files)

    code = app.exec()
    config.save()
    log.info("Exit with code %s", code)
    return code

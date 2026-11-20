"""Creates the QApplication, applies the theme and opens the main window."""

from __future__ import annotations

import logging

from PySide6.QtWidgets import QApplication

from app.config import APP_NAME, APP_VERSION, AppConfig
from app.logging_setup import setup_logging
from core.media.errors import FFmpegNotFoundError
from core.media.ffmpeg_locator import FFmpegPaths, locate_ffmpeg

log = logging.getLogger(__name__)



def load_theme() -> str:
    from ui.theme import stylesheet

    return stylesheet()


def create_application(argv: list[str], theme: str = "dark", mica: bool = False) -> QApplication:
    """QApplication with the Twitch-coloured Material 3 look. The theme must be chosen before any
    other UI module is imported, because widgets read the colours then."""
    from ui.theme import palette

    palette.set_theme(theme)
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    from ui.theme import windows

    windows.install(app, mica=mica)
    app.setStyleSheet(load_theme() + (windows.MICA_QSS if windows.mica_enabled() else ""))
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

    app = create_application(argv, theme=config.theme, mica=config.mica)

    # Imported here so that logging is ready before any UI module loads.
    from ui.main_window.main_window import MainWindow

    window = MainWindow(config=config, ffmpeg=find_ffmpeg(config))
    window.show()

    files = [arg for arg in argv[1:] if not arg.startswith("-")]
    if files:
        window.import_files(files)
    else:
        from PySide6.QtCore import QTimer

        # Unsaved work left by a crash: offer it once the window is on screen.
        QTimer.singleShot(300, window.session.offer_recovery)

    code = app.exec()
    config.save()
    log.info("Exit with code %s", code)
    if getattr(window, "restart_requested", False):
        restart()
    return code


def restart() -> bool:
    """Start a fresh copy of the app (used after the theme was changed)."""
    import sys
    from pathlib import Path

    from PySide6.QtCore import QProcess

    if getattr(sys, "frozen", False):
        return QProcess.startDetached(sys.executable, sys.argv[1:])
    root = str(Path(__file__).resolve().parent.parent)
    ok, _pid = QProcess.startDetached(sys.executable, ["-m", "app.main"], root)
    return bool(ok)

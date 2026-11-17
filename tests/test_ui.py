"""GUI smoke tests (run with QT_QPA_PLATFORM=offscreen)."""

from __future__ import annotations

import time

from PySide6.QtCore import QRect, QSize, QThreadPool
from PySide6.QtWidgets import QApplication

from app.bootstrap import create_application
from app.config import AppConfig
from ui.preview.video_canvas import fit_rect


def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _window(data_dir, ffmpeg):
    from ui.main_window.main_window import MainWindow

    return MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg)


def test_fit_rect_letterboxes():
    r = fit_rect(QSize(1920, 1080), QRect(0, 0, 1000, 1000))
    assert round(r.width()) == 1000 and round(r.height()) == 562
    assert round(r.y()) == 219


def test_theme_loads(qapp):
    app = create_application([])
    assert "QDockWidget" in app.styleSheet()


def test_main_window_has_panels(qapp, data_dir):
    window = _window(data_dir, None)
    titles = {d.windowTitle() for d in (window.media_dock, window.properties_dock, window.timeline_dock)}
    assert titles == {"Media", "Properties", "Timeline"}
    assert window.centralWidget() is window.preview
    window.close()


def test_import_in_background_and_show_properties(qapp, data_dir, ffmpeg_paths, sample_clips):
    window = _window(data_dir, ffmpeg_paths)
    window.show()
    window.import_files([str(sample_clips["mp4"]), str(sample_clips["mkv_noaudio"])])

    assert _wait(lambda: window.media_panel.list.count() == 2)
    QThreadPool.globalInstance().waitForDone(5000)
    names = sorted(m.path.name for m in window.media_panel.all_media())
    assert names == ["silent.mkv", "stream clip.mp4"]
    assert len(window.project.media) == 2

    # Select the mp4 and check properties panel content.
    for i in range(window.media_panel.list.count()):
        item = window.media_panel.list.item(i)
        if "stream clip" in item.text():
            window.media_panel.list.setCurrentItem(item)
    rows = window.properties_panel.rows()
    assert rows["Resolution"] == "1280 × 720"
    assert rows["Frame rate"] == "30"
    assert window.preview.media is not None

    # Removing media updates the project.
    window.media_panel.remove_button.click()
    assert len(window.project.media) == 1
    window._dirty = False
    window.close()


def test_import_failure_is_reported(qapp, data_dir, ffmpeg_paths, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a))
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"\x00" * 100)

    window = _window(data_dir, ffmpeg_paths)
    window.import_files([str(bad)])
    assert _wait(lambda: bool(shown))
    assert window.media_panel.list.count() == 0
    window.close()


def test_save_and_reopen_project(qapp, data_dir, ffmpeg_paths, sample_clips, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    window = _window(data_dir, ffmpeg_paths)
    window.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: window.media_panel.list.count() == 1)

    target = tmp_path / "session.tssproj"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    assert window.save()
    assert target.is_file()
    assert "*" not in window.windowTitle()
    window.close()

    other = _window(data_dir, ffmpeg_paths)
    other.open_project(target)
    assert _wait(lambda: other.media_panel.list.count() == 1)
    assert other.project.name == "session"
    other.close()


def test_theme_icons_resolve(qapp):
    import re
    from pathlib import Path

    from app.bootstrap import load_theme
    from ui.theme import icon

    qss = load_theme()
    assert "@" + "ACCENT@" not in qss and "@GLYPHS@" not in qss
    urls = re.findall(r'url\("([^"]+)"\)', qss)
    assert urls and all(Path(u).is_file() for u in urls)
    for name in ("import", "folder", "save", "undo", "redo", "export", "youtube", "play", "pause", "prev",
                 "next", "volume", "library", "sparkle"):
        assert not icon(name).isNull(), name
    assert icon("missing-icon").isNull()


def test_fluent_accent(monkeypatch):
    from ui.theme import palette

    light = palette.make_accent("#60cdff")
    assert light.on_fill == "#000000" and light.fill == "#60cdff"
    dark = palette.make_accent("#0063b1")
    assert dark.on_fill == "#ffffff" and palette.luminance(dark.text) > palette.luminance(dark.fill)
    assert palette.mix("#000000", "#ffffff", 0.5) == "#808080"
    palette.accent.cache_clear()
    monkeypatch.setenv("TSS_ACCENT", "#ff8800")
    try:
        assert palette.accent().fill == "#ff8800"
    finally:
        palette.accent.cache_clear()


def test_main_toolbar_actions(qapp, data_dir):
    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=None)
    bar = w.main_toolbar
    assert bar.widgetForAction(w.act_export).objectName() == "primaryAction"
    assert bar.widgetForAction(w.act_upload).objectName() == "youtubeAction"
    assert "Untitled" in w.project_label.text()
    w._set_dirty(True)
    assert "unsaved" in w.project_label.text()
    w._dirty = False
    w.close()

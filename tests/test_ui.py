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
    assert window.page_stack.currentWidget() is window.preview
    window.close()


def test_step_pages(qapp, data_dir, ffmpeg_paths, sample_clips):
    from ui.main_window.pages import DATA, EDIT, PUBLISH, RENDER

    w = _window(data_dir, ffmpeg_paths)
    w.show()
    qapp.processEvents()
    assert w.page == EDIT and w.media_dock.isVisible() and not w.page_bar.is_done(EDIT)
    assert not w.render_page.action_button.isEnabled()
    w.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: w.timeline.timeline is not None)
    assert w.page_bar.is_done(EDIT) and not w.page_bar.is_done(DATA)

    w.page_bar.next_button.click()  # Edit → Data
    assert w.page == DATA and w.page_stack.currentWidget() is w.data_page
    assert not w.media_dock.isVisible() and not w.timeline_dock.isVisible()
    w.metadata_panel.title_edit.setText("Мой шорт")
    assert w.page_bar.is_done(DATA)
    w.page_actions[RENDER].trigger()  # Ctrl+3
    assert w.page == RENDER and w.render_page.action_button.isEnabled()
    assert "Length of the Short" in w.render_page.status.text()

    # An export marks Render done until the edit changes.
    out = data_dir / "short.mp4"
    out.write_bytes(b"x")
    w._rendered = (out, w._render_state())
    w._refresh_steps()
    assert w.page_bar.is_done(RENDER) and "short.mp4" in w.publish_page.status.text()
    w._published = str(out)
    w.set_page(PUBLISH)
    assert w.page_bar.is_done(PUBLISH) and w.page_bar.next_button.isHidden()

    w.set_page(EDIT)
    assert w.media_dock.isVisible() and w.timeline_dock.isVisible()  # the editor's docks come back
    w.timeline.set_playhead(1_000_000)
    w.act_split.trigger()
    assert not w.page_bar.is_done(RENDER)  # edited after the export
    w._set_dirty(False)
    w.close()


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


def test_jump_slider_click_anywhere(qapp):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from app.bootstrap import load_theme
    from ui.widgets.jump_slider import JumpSlider

    qapp.setStyleSheet(load_theme())
    try:
        slider = JumpSlider()
        slider.setRange(0, 1000)
        slider.resize(400, 36)
        slider.show()
        qapp.processEvents()
        assert slider.height() >= 36
        moved, released = [], []
        slider.sliderMoved.connect(moved.append)
        slider.sliderReleased.connect(lambda: released.append(slider.value()))
        # Click near the top edge, far from the thin track: still jumps there.
        QTest.mouseClick(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(300, 2))
        assert 700 <= slider.value() <= 800 and moved and released == [slider.value()]
        QTest.mouseClick(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(1, 34))
        assert slider.value() <= 20
        # Press off the track, drag along it, release: one scrub.
        QTest.mousePress(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(100, 30))
        assert slider.isSliderDown()
        QTest.mouseMove(slider, QPoint(200, 30))
        QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(200, 30))
        assert not slider.isSliderDown() and 450 <= slider.value() <= 550 and released[-1] == slider.value()
        slider.close()
    finally:
        qapp.setStyleSheet("")


def test_tabbed_docks_have_no_duplicate_title(qapp, data_dir):
    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=None)
    w.show()
    qapp.processEvents()
    # Properties, Transcript, Censor and Metadata share one tab group: the tab names them.
    for dock in (w.properties_dock, w.transcript_dock, w.censor_dock, w.subtitles_dock):
        assert dock.titleBarWidget() is not None and dock.titleBarWidget().height() == 0
    assert w.media_dock.titleBarWidget() is None  # a lone dock keeps its title
    w.censor_dock.setFloating(True)
    qapp.processEvents()
    assert w.censor_dock.titleBarWidget() is None  # floating: needs its title bar to be moved
    w.reset_layout()
    assert w.censor_dock.titleBarWidget() is not None
    # Undo / redo only in the command bar, not again above the timeline.
    from PySide6.QtWidgets import QToolButton

    actions = {b.defaultAction() for b in w.timeline_panel.findChildren(QToolButton)}
    assert w.act_undo not in actions and w.act_split in actions
    assert not w.act_split.icon().isNull()
    w._dirty = False
    w.close()


def test_media_panel_empty_state_and_thumbnail(qapp, sample_clips, ffmpeg_paths, tmp_path):
    from PySide6.QtGui import QImage

    from core.media.probe import probe_media
    from core.media.thumbnail import make_thumbnail, thumbnail_path
    from ui.panels.media_panel import MediaPanel

    panel = MediaPanel()
    assert panel.stack.currentWidget() is panel.empty
    requested = []
    panel.import_requested.connect(lambda: requested.append(1))
    panel.empty.button.click()
    assert requested == [1]

    clip = next(iter(sample_clips.values()))
    info = probe_media(clip, ffmpeg_paths.ffprobe)
    panel.add_media(info)
    assert panel.stack.currentIndex() == 1 and panel.count_label.text() == "1 video"
    assert ":" in MediaPanel.details(info)

    out = make_thumbnail(ffmpeg_paths.ffmpeg, clip, info.duration_s, tmp_path)
    assert out == thumbnail_path(clip, tmp_path) and out.is_file()
    assert make_thumbnail(ffmpeg_paths.ffmpeg, clip, info.duration_s, tmp_path) == out  # cached
    panel.set_thumbnail(clip, QImage(str(out)))
    assert panel.thumbnail(clip) is not None and not panel.thumbnail(clip).isNull()
    panel.resize(260, 300)
    panel.grab()  # the delegate paints the thumbnail
    assert make_thumbnail(ffmpeg_paths.ffmpeg, tmp_path / "missing.mp4", 1.0, tmp_path) is None

    panel.remove_media(clip)
    assert panel.stack.currentWidget() is panel.empty


def test_cards_in_long_forms(qapp, tmp_path):
    from core.editor.presets import PresetStore
    from ui.editor.composition_controller import CompositionController
    from ui.panels.composition_panel import CompositionPanel
    from ui.widgets.card import Card

    panel = CompositionPanel(CompositionController(), PresetStore(tmp_path / "presets.json"))
    titles = [c.title_label.text() for c in panel.findChildren(Card) if not c.isHidden()]  # "Part" shows after Split
    assert titles == ["Preset", "Region", "Placement in output", "Layout", "Face"]


def test_light_theme_translates_colours():
    from ui.theme import palette, stylesheet

    try:
        assert palette.set_theme("light") == "light"
        assert palette.is_light() and palette.BASE == "#f3f3f3" and palette.TEXT == "#1b1b1b"
        assert palette.themed("#2d2d2d") == "#fdfdfd" and palette.themed("#ff4d9d") == "#ff4d9d"
        qss = stylesheet()
        assert "#202020" not in qss and "#f3f3f3" in qss and "@" not in qss.replace("@media", "")
        assert palette.accent().on_fill in ("#ffffff", "#000000")
        assert palette.resolve_theme("system") in ("dark", "light")
    finally:
        palette.set_theme("dark")
    assert palette.BASE == "#202020" and "#202020" in stylesheet()


def test_appearance_setting_is_saved(qapp, data_dir):
    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow

    cfg = AppConfig.load(data_dir)
    w = MainWindow(config=cfg, ffmpeg=None)
    assert w.theme_actions["dark"].isChecked() and not w.act_mica.isChecked()
    assert w.set_appearance(theme="light", ask=False)
    assert not w.set_appearance(theme="light", ask=False)  # nothing changed
    assert AppConfig.load(data_dir).theme == "light"
    w._dirty = False
    w.close()


def test_view_settings_survive_reload(data_dir):
    from app.config import AppConfig

    cfg = AppConfig.load(data_dir)
    cfg.theme, cfg.mica, cfg.timeline_view = "system", True, {"wave_gain": 2.0}
    cfg.save()
    again = AppConfig.load(data_dir)
    assert (again.theme, again.mica, again.timeline_view) == ("system", True, {"wave_gain": 2.0})

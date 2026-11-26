"""Stage 8: thumbnails (cover of the Short)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.editor.composition import default_composition
from core.projects.project import Project
from core.thumbnails.model import LANDSCAPE, ThumbnailDesign, clip_title


def test_design_round_trip_and_clean():
    design = ThumbnailDesign(source_us=1_500_000, text="Охота", style="purple_box", position="bottom",
                             size=180, zoom=1.4, format=LANDSCAPE)
    assert ThumbnailDesign.from_dict(design.to_dict()) == design
    assert ThumbnailDesign().to_dict() == {}
    wild = ThumbnailDesign.from_dict({"style": "nope", "size": 9999, "zoom": 0.1, "position": "x", "junk": 1})
    assert wild.style == "yellow" and wild.size == 260 and wild.zoom == 1.0 and wild.position == "top"
    assert ThumbnailDesign.from_dict(None) == ThumbnailDesign()


def test_clip_title_and_shown_text():
    assert clip_title("КОНСТРУКТИВНАЯ ОХОТА | #sasavot on #Twitch") == "КОНСТРУКТИВНАЯ ОХОТА"
    assert clip_title("смешной момент #shorts") == "смешной момент"
    assert ThumbnailDesign().shown_text("охота | x") == "ОХОТА"
    assert ThumbnailDesign(text="Своё", uppercase=False).shown_text("охота") == "Своё"


def test_project_keeps_thumbnail():
    project = Project(thumbnail=ThumbnailDesign(text="cover", source_us=10))
    again = Project.from_dict(project.to_dict())
    assert again.thumbnail == project.thumbnail
    assert "thumbnail" not in Project().to_dict()


def test_draw_from_real_frame(qapp, sample_clips, ffmpeg_paths, tmp_path):
    from core.thumbnails.draw import MAX_BYTES, jpeg_bytes, make_thumbnail, save_thumbnail
    from core.thumbnails.frame import grab_frame

    png = grab_frame(ffmpeg_paths.ffmpeg, sample_clips["mp4"], 1_000_000)
    assert png.startswith(b"\x89PNG")
    comp = default_composition(1280, 720)
    image = make_thumbnail(png, comp, 1280, 720, ThumbnailDesign(), "Охота | #sasavot")
    assert (image.width(), image.height()) == (1080, 1920)
    plain = make_thumbnail(png, comp, 1280, 720, ThumbnailDesign(text=" ", uppercase=False), "")
    assert image != plain  # the title was drawn
    wide = make_thumbnail(png, comp, 1280, 720, ThumbnailDesign(format=LANDSCAPE, style="red_box"), "Охота")
    assert (wide.width(), wide.height()) == (1280, 720)
    data = jpeg_bytes(image)
    assert data[:2] == b"\xff\xd8" and len(data) <= MAX_BYTES
    assert save_thumbnail(image, tmp_path / "a" / "t.jpg").stat().st_size > 0
    assert save_thumbnail(image, tmp_path / "t.png").read_bytes()[:4] == b"\x89PNG"


def test_editor_edits_design(qapp):
    from ui.thumbnails.thumbnail_editor import ThumbnailEditor

    editor = ThumbnailEditor()
    seen = []
    editor.design_changed.connect(seen.append)
    editor.set_title("ОХОТА | #sasavot on #Twitch")
    assert editor.text_edit.placeholderText() == "ОХОТА"
    editor.style_combo.setCurrentIndex(editor.style_combo.findData("red_box"))
    editor.size_slider.setValue(200)
    editor.text_edit.textEdited.emit("Своё")
    assert seen[-1] == ThumbnailDesign(style="red_box", size=200, text="Своё")
    editor.enabled_check.setChecked(False)
    assert not seen[-1].enabled and not editor.style_combo.isEnabled()
    editor.set_design(ThumbnailDesign(source_us=2_000_000))
    assert "0:02.0" in editor.frame_label.text()


def test_data_page_thumbnail(qapp, data_dir, ffmpeg_paths, sample_clips, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from tests.test_steps import _wait, _window
    from ui.main_window.pages import DATA

    window = _window(data_dir, ffmpeg_paths)
    try:
        window.import_files([str(sample_clips["mp4"])])
        assert _wait(lambda: window.preview.media is not None)
        window.set_page(DATA)
        assert _wait(lambda: window.thumbnails.image is not None)
        assert window.thumbnails.pixmap() is not None
        window.composition.set_time(1_000_000)
        window.thumbnail_editor.frame_button.click()
        assert window.project.thumbnail.source_us == 1_000_000
        assert _wait(lambda: window.thumbnails.image is not None and window.thumbnails._frame[0][1] == 1_000_000)
        make = window.thumbnails.job()
        assert make is not None and make()[:2] == b"\xff\xd8"
        out = tmp_path / "cover.jpg"
        offered = []
        monkeypatch.setattr(QFileDialog, "getSaveFileName",
                            lambda *a, **k: (offered.append(a[2]), (str(out), ""))[1])
        window.metadata_panel.title_edit.setText("Охота | #sasavot on #Twitch")
        window.thumbnail_editor.save_button.click()
        assert out.is_file()
        assert Path(offered[0]).name == "Охота.jpg"
        window.thumbnail_editor.enabled_check.setChecked(False)
        assert window.thumbnails.job() is None
    finally:
        window._set_dirty(False)
        window.close()


def test_candidate_times():
    from core.thumbnails.draw import candidate_times

    times = candidate_times(12_000_000, lambda out: out + 5_000_000, peaks=[9_000_000, 9_500_000, 15_000_000])
    assert 9_000_000 in times and 15_000_000 in times and 9_500_000 not in times  # peaks first, 1 s apart
    assert len(times) == 6 and times == sorted(times)
    assert all(abs(a - b) >= 1_000_000 for a, b in zip(times, times[1:]))
    assert candidate_times(0, lambda out: out) == [0]


def test_thumbnail_frame_choices(qapp, data_dir, ffmpeg_paths, sample_clips):
    from tests.test_steps import _wait, _window
    from ui.main_window.pages import DATA

    window = _window(data_dir, ffmpeg_paths)
    try:
        window.import_files([str(sample_clips["mp4"])])
        assert _wait(lambda: window.timeline.timeline is not None)
        window.set_page(DATA)
        editor = window.thumbnail_editor
        assert _wait(lambda: len(editor.frame_buttons) >= 2)
        second = editor.frame_buttons[1]
        second.click()
        assert window.project.thumbnail.source_us == second.property("source_us") and second.isChecked()
        assert editor.scrub.maximum() == 2000  # the Short is 2 s
        editor.scrub.setValue(1500)
        assert _wait(lambda: window.project.thumbnail.source_us == 1_500_000)
        assert not any(b.isChecked() for b in editor.frame_buttons)
    finally:
        window._set_dirty(False)
        window.close()


def test_file_name_from_clip_title(tmp_path):
    from ui.export.export_dialog import default_output_path, title_file_name

    assert title_file_name("ОГРАБЛЕНИЕ/САСЫЧА | #sasavot on #Twitch") == "ОГРАБЛЕНИЕ-САСЫЧА"
    assert title_file_name('Что?! "да" : нет') == "Что! да нет"
    source = tmp_path / "clip.mp4"
    assert default_output_path(source, str(tmp_path), ".mp4", "Охота | #sasavot").name == "Охота.mp4"
    (tmp_path / "Охота.mp4").write_bytes(b"x")
    assert default_output_path(source, str(tmp_path), ".mp4", "Охота | x").name == "Охота_2.mp4"
    assert default_output_path(source, str(tmp_path), ".mp4", "").name == "clip_short.mp4"


def test_cover_file_name_from_clip_title():
    from ui.main_window.thumbnail_keeper import cover_file_name

    assert cover_file_name("  ОГРАБЛЕНИЕ/САСЫЧА?  | #sasavot on #Twitch", "C:/v/clip.mp4") == "ОГРАБЛЕНИЕ-САСЫЧА.jpg"
    assert cover_file_name("Охота: начало", "C:/v/clip.mp4") == "Охота начало.jpg"
    assert cover_file_name("", "C:/v/clip.mp4") == "clip_thumbnail.jpg"
    assert cover_file_name(" | #sasavot", "C:/v/clip.mp4") == "clip_thumbnail.jpg"


def test_render_page_name_follows_title(qapp, data_dir, ffmpeg_paths, sample_clips, tmp_path):
    from tests.test_steps import _wait, _window
    from ui.main_window.pages import DATA, RENDER

    w = _window(data_dir, ffmpeg_paths)
    try:
        w.import_files([str(sample_clips["mp4"])])
        assert _wait(lambda: w.timeline.timeline is not None)
        w.set_page(RENDER)
        form = w.export_widget
        assert form.name_edit.text() == "stream clip_short"
        w.set_page(DATA)
        w.metadata_panel.title_edit.setText("ОГРАБЛЕНИЕ/САСЫЧА | #sasavot on #Twitch")
        w.set_page(RENDER)
        assert form.name_edit.text() == "ОГРАБЛЕНИЕ-САСЫЧА"
        form.name_edit.setText("моё имя")
        w.set_page(DATA)
        w.metadata_panel.title_edit.setText("Другое | x")
        w.set_page(RENDER)
        assert form.name_edit.text() == "моё имя"  # a typed name stays
    finally:
        w._set_dirty(False)
        w.close()


def test_blocks_can_be_turned_off(qapp, data_dir, ffmpeg_paths):
    from app.config import AppConfig
    from tests.test_steps import _window

    w = _window(data_dir, ffmpeg_paths)
    key = next(k for k in w.block_actions if k.endswith("/Zoom & pan"))
    card = w.composition_panel.zoom_card
    assert not card.isHidden()
    w.block_actions[key].setChecked(False)
    assert card.isHidden() and key in AppConfig.load(data_dir).hidden_blocks
    w.composition_panel.refresh()  # the panel's own show/hide does not bring it back
    face = w.composition_panel.face_card
    face_key = next(k for k in w.block_actions if k.endswith("/Face"))
    w.block_actions[face_key].setChecked(False)
    w.composition.update_region("webcam", enabled=True)
    assert face.isHidden()
    assert "Data page/Thumbnail" in w.block_actions
    w._set_dirty(False)
    w.close()

    again = _window(data_dir, ffmpeg_paths)
    assert again.composition_panel.zoom_card.isHidden()
    assert not again.block_actions[key].isChecked()
    again._show_all_blocks()
    assert not again.composition_panel.zoom_card.isHidden() and AppConfig.load(data_dir).hidden_blocks == []
    again._set_dirty(False)
    again.close()


def test_text_can_be_moved_up_and_down(qapp):
    from PySide6.QtGui import QColor, QImage

    from core.thumbnails.draw import draw_thumbnail
    from core.thumbnails.model import MIDDLE, ThumbnailDesign
    from ui.thumbnails.thumbnail_editor import ThumbnailEditor

    frame = QImage(1080, 1920, QImage.Format.Format_RGB32)
    frame.fill(QColor("#000000"))

    def text_rows(design):
        image = draw_thumbnail(frame, design, "ТЕСТ").scaled(108, 192)
        rows = [y for y in range(image.height())
                if any(QColor(image.pixel(x, y)).red() > 200 for x in range(image.width()))]
        return min(rows), max(rows)

    base = ThumbnailDesign(position=MIDDLE, shade=False)
    middle = text_rows(base)
    lower = text_rows(ThumbnailDesign(position=MIDDLE, shade=False, offset=30))
    higher = text_rows(ThumbnailDesign(position=MIDDLE, shade=False, offset=-30))
    assert lower[0] > middle[0] + 40 and higher[0] < middle[0] - 40
    bottom = text_rows(ThumbnailDesign(position=MIDDLE, shade=False, offset=45, size=260))
    assert bottom[1] < 192  # kept on the picture
    assert ThumbnailDesign.from_dict({"offset": 400}).offset == 45
    assert ThumbnailDesign.from_dict(ThumbnailDesign(offset=-12).to_dict()).offset == -12

    editor = ThumbnailEditor()
    seen = []
    editor.design_changed.connect(seen.append)
    editor.offset_slider.setValue(-20)
    assert seen[-1].offset == -20 and editor.offset_label.text() == "↑ 20%"

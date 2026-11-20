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
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
        window.thumbnail_editor.save_button.click()
        assert out.is_file()
        window.thumbnail_editor.enabled_check.setChecked(False)
        assert window.thumbnails.job() is None
    finally:
        window._set_dirty(False)
        window.close()

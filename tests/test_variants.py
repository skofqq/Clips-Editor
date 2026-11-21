"""Title and cover variants on the Data page."""

from __future__ import annotations

from core.metadata.generate import MetadataSelection
from core.metadata.library import GAME, PARTICIPANT, STREAMER, TEMPLATE, Library
from core.metadata.variants import AS_TYPED, CAPS, STREAMER_FIRST, WITH_GAME, WITH_PEOPLE, title_variants
from core.thumbnails.model import ThumbnailDesign
from core.thumbnails.variants import cover_variants, render_variants


def test_title_variants_keep_the_template():
    lib = Library(None)
    try:
        streamer = lib.create(STREAMER, "sasavot")
        helin = lib.create(PARTICIPANT, "helin139")
        game = lib.create(GAME, "CS2")
        lib.create(TEMPLATE, "Short", title="{clip_title}")
        selection = MetadataSelection(streamer_id=streamer.id, participant_ids=(helin.id,), game_id=game.id,
                                      clip_title="Клатч на 1 хп")
        variants = title_variants(lib, selection)
        keys = [v.key for v in variants]
        assert keys[:2] == [AS_TYPED, CAPS]
        assert {STREAMER_FIRST, WITH_GAME, WITH_PEOPLE} <= set(keys)
        assert any(k.startswith("template:") for k in keys)
        titles = [v.title for v in variants]
        assert len(set(titles)) == len(titles)  # no repeats
        assert all(len(t) <= 100 for t in titles)
        caps = variants[1].title
        assert "КЛАТЧ НА 1 ХП" in caps
        assert any(t.startswith("sasavot: Клатч") for t in titles)
        assert title_variants(lib, MetadataSelection()) == [] or all(
            v.key.startswith("template:") for v in title_variants(lib, MetadataSelection()))
    finally:
        lib.close()


def test_cover_variants_mix_frames_and_styles(qapp):
    from PySide6.QtGui import QColor, QImage

    design = ThumbnailDesign(style="yellow")
    variants = cover_variants(design, [1_000_000, 2_000_000, 3_000_000, 4_000_000])
    assert len(variants) == 6
    assert {v.design.style for v in variants} == {"yellow", "purple_box"}
    assert [v.design.source_us for v in variants[::2]] == [1_000_000, 2_000_000, 3_000_000]
    frame = QImage(90, 160, QImage.Format.Format_RGB32)
    frame.fill(QColor("#336699"))
    images = render_variants(variants, {1_000_000: frame}, "Клатч", height=120)
    assert images[0].height() == 120 and images[2].isNull()


def test_data_page_variants_card(qapp, data_dir, ffmpeg_paths, sample_clips):
    from tests.test_steps import _wait, _window
    from ui.main_window.pages import DATA

    w = _window(data_dir, ffmpeg_paths)
    try:
        w.import_files([str(sample_clips["mp4"])])
        assert _wait(lambda: w.preview.media is not None)
        w.metadata_panel.clip_title_edit.setText("Клатч на 1 хп")
        w.set_page(DATA)
        # Two styles for each suggested frame (a 2 s clip has two), at most three frames.
        assert _wait(lambda: w.thumbnails.candidates and len(w.variants_card.cover_buttons)
                     == 2 * min(3, len(w.thumbnails.candidates)), 20)
        assert _wait(lambda: len(w.variants_card.title_buttons) >= 2)
        w.variants_card.title_buttons[1].click()
        assert w.metadata_panel.metadata().title == w.variants_card.title_texts()[1]
        assert w.variants_card.title_buttons[1].isChecked()
        assert w._variant_text().startswith("Capitals")
        w.variants_card.cover_buttons[1].click()
        assert w.project.thumbnail.style == "purple_box"
        assert w.variants_card.cover_buttons[1].isChecked()
        assert "White on Twitch purple" in w._variant_text()
    finally:
        w._set_dirty(False)
        w.close()

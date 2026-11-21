"""Animated caption styles (0.9.26): karaoke sweep / fill, word by word, slide,
key-word colour, emoji place and per-caption emoji, the user's own styles."""

from __future__ import annotations

import numpy as np
import pytest

from core.subtitles.emoji import emoji_match, is_key_word
from core.subtitles.model import (
    ANIM_REVEAL,
    ANIM_SLIDE,
    EMOJI_BELOW,
    EMOJI_INLINE,
    KARAOKE_FILL,
    KARAOKE_SWEEP,
    KARAOKE_WORD,
    PRESETS,
    Caption,
    CaptionWord,
    SubtitleEdits,
    SubtitleStyle,
    build_captions,
    style_from_preset,
)
from core.timeline.model import Timeline
from core.transcription.model import Segment, Transcript, Word

S = 1_000_000


def _caption(*texts: str, start: float = 1.0, emoji: str | None = None) -> Caption:
    words = tuple(CaptionWord(t, int((start + i * 0.5) * S), int((start + i * 0.5 + 0.4) * S), i)
                  for i, t in enumerate(texts))
    return Caption(words[0].start_us, words[-1].end_us + 300_000, words, emoji)


def _draw(style: SubtitleStyle, cap: Caption, now_us: int) -> np.ndarray:
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter

    from core.subtitles.raster import draw_caption

    image = QImage(540, 960, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    draw_caption(painter, QRectF(0, 0, 540, 960), cap, style, now_us, 0.5)
    painter.end()
    return np.frombuffer(image.constBits(), dtype=np.uint8).reshape(960, 540, 4).copy()


def _green(img: np.ndarray) -> np.ndarray:
    return (img[:, :, 1] > 200) & (img[:, :, 0] < 80) & (img[:, :, 2] < 80)  # BGRA


def test_new_fields_round_trip_and_clean():
    style = SubtitleStyle(enabled=True, karaoke_mode=KARAOKE_SWEEP, dim_upcoming=True, emphasis=True,
                          emphasis_color="#ff00ff", emoji_place=EMOJI_INLINE, animation=ANIM_REVEAL)
    assert SubtitleStyle.from_dict(style.to_dict()) == style
    odd = SubtitleStyle.from_dict({"karaoke_mode": "rainbow", "emoji_place": "left", "animation": "spin"})
    assert (odd.karaoke_mode, odd.emoji_place) == (KARAOKE_WORD, "above")
    old = SubtitleStyle()  # projects from before 0.9.26 look the same
    assert (old.karaoke_mode, old.dim_upcoming, old.emphasis) == (KARAOKE_WORD, False, False)
    for name in ("Karaoke sweep", "Word by word", "Key words", "Fade in, fill", "Slide box"):
        assert PRESETS[name].enabled and PRESETS[name] == PRESETS[name].cleaned()


def test_preset_keeps_position_and_mask():
    current = SubtitleStyle(position=0.3, mask="all")
    style = style_from_preset(PRESETS["Karaoke sweep"], current)
    assert (style.position, style.mask, style.karaoke_mode) == (0.3, "all", KARAOKE_SWEEP)


def test_caption_emoji_edits():
    edits = SubtitleEdits().split_at(7).with_emoji(5, "🐐").with_emoji(9, "")
    assert edits.emoji_at(5) == "🐐" and edits.emoji_at(9) == "" and edits.emoji_at(1) is None
    assert SubtitleEdits.from_dict(edits.to_dict()) == edits
    assert edits.with_emoji(5, None).emoji_at(5) is None and not edits.empty
    assert SubtitleEdits().with_emoji(5, "x").with_emoji(5, None).empty
    assert SubtitleEdits.from_dict({"emoji": {"oops": "x", "3": 4}}).emoji == ()
    # Splits and joins keep the chosen emoji.
    assert edits.join_at(11).emoji == edits.emoji

    words = (Word(0, 400_000, "привет"), Word(500_000, 900_000, "ребята"))
    transcript = Transcript("ru", 0.9, "small", (Segment(0, 0, 900_000, "привет ребята", words),), S)
    timeline = Timeline.for_source(S)
    style = SubtitleStyle(enabled=True, uppercase=False)
    caps = build_captions(transcript, timeline, style, SubtitleEdits().with_emoji(0, "🔥"))
    assert caps[0].emoji == "🔥"
    assert build_captions(transcript, timeline, style)[0].emoji is None


def test_emoji_match_and_key_words():
    assert emoji_match(["это", "просто", "жесть"]) == ("🔥", 2)
    assert emoji_match(["АХАХА", "клатч"]) == ("😂", 0)
    assert emoji_match(["привет"]) == ("", -1)
    assert emoji_match(["no", "way"]) == ("😱", 1)
    assert is_key_word("ЖЕСТЬ") and is_key_word("100") and is_key_word("клатч!")
    assert not is_key_word("привет") and not is_key_word("б***") and not is_key_word("")


def test_scales_and_fractions():
    from core.subtitles.raster import POP_US, caption_slide, emoji_scale, reveal_scale, sweep_fraction

    cap = _caption("раз", "два", "жесть")
    reveal = SubtitleStyle(enabled=True, animation=ANIM_REVEAL)
    assert reveal_scale(reveal, cap, 2, cap.words[1].start_us) == 0.0
    assert reveal_scale(reveal, cap, 2, cap.words[2].start_us + POP_US) == 1.0
    assert reveal_scale(SubtitleStyle(), cap, 2, cap.start_us) == 1.0
    slide = SubtitleStyle(enabled=True, animation=ANIM_SLIDE)
    offset, alpha = caption_slide(slide, cap, cap.start_us)
    assert offset > 0.5 and alpha == 0.0
    assert caption_slide(slide, cap, cap.start_us + POP_US) == (0.0, 1.0)
    assert sweep_fraction(cap, 0, cap.words[0].start_us + 200_000) == pytest.approx(0.5)
    assert sweep_fraction(cap, 0, cap.words[0].end_us + 1) == 1.0
    inline = SubtitleStyle(enabled=True, emoji=True, emoji_place=EMOJI_INLINE, animation=ANIM_REVEAL)
    assert emoji_scale(inline, cap, cap.words[1].start_us) == 0.0  # «жесть» is not spoken yet
    assert emoji_scale(inline, cap, cap.words[2].start_us + POP_US) == 1.0


def test_frame_times_for_new_styles():
    from core.subtitles.raster import FRAME_US, frame_times

    cap = _caption("раз", "два", "три")
    sweep = SubtitleStyle(enabled=True, karaoke_mode=KARAOKE_SWEEP)
    times = frame_times(cap, sweep)
    inside = [t for t in times if cap.words[0].start_us < t < cap.words[0].end_us]
    assert len(inside) >= 400_000 // FRAME_US - 1  # the colour runs through the word
    reveal = frame_times(cap, SubtitleStyle(enabled=True, animation=ANIM_REVEAL, karaoke=False))
    assert all(w.start_us in reveal for w in cap.words) and len(reveal) > 3 * 3
    slide = frame_times(cap, SubtitleStyle(enabled=True, animation=ANIM_SLIDE, karaoke=False))
    assert len(slide) > 3 and max(slide) < cap.words[1].start_us
    dim = frame_times(cap, SubtitleStyle(enabled=True, karaoke=False, dim_upcoming=True))
    assert dim == [w.start_us for w in cap.words]
    hot = _caption("это", "жесть")
    inline = frame_times(hot, SubtitleStyle(enabled=True, karaoke=False, emoji=True, emoji_place=EMOJI_INLINE))
    assert hot.words[1].start_us in inline  # the emoji appears with its word


def test_sweep_colours_part_of_the_word(qapp):
    cap = _caption("ВОТ", "ЭТО")
    style = SubtitleStyle(enabled=True, highlight="#00ff00", karaoke_mode=KARAOKE_SWEEP, shadow=False)
    word = cap.words[1]
    early = _green(_draw(style, cap, word.start_us + 80_000)).sum()
    half = _green(_draw(style, cap, word.start_us + 200_000)).sum()
    full = _green(_draw(style, cap, word.end_us - 1)).sum()
    assert early < half < full
    # "Stay coloured" colours the whole spoken word at once (and, like the sweep, the words before it).
    whole = _green(_draw(SubtitleStyle(enabled=True, highlight="#00ff00", shadow=False, karaoke_mode=KARAOKE_FILL),
                         cap, word.start_us + 80_000)).sum()
    assert whole > early * 1.5 and whole == pytest.approx(full, rel=0.05)


def test_fill_keeps_spoken_words_coloured(qapp):
    cap = _caption("ВОТ", "ЭТО", "ДА")
    now = cap.words[2].start_us + 10_000
    word = _green(_draw(SubtitleStyle(enabled=True, highlight="#00ff00", shadow=False), cap, now)).sum()
    fill = _green(_draw(SubtitleStyle(enabled=True, highlight="#00ff00", shadow=False,
                                      karaoke_mode=KARAOKE_FILL), cap, now)).sum()
    assert fill > word * 2


def test_reveal_and_dim_draw_less(qapp):
    cap = _caption("РАЗ", "ДВА", "ТРИ")
    now = cap.words[0].start_us + 300_000
    full = _draw(SubtitleStyle(enabled=True, shadow=False, karaoke=False), cap, now)[:, :, 3].sum()
    reveal = _draw(SubtitleStyle(enabled=True, shadow=False, karaoke=False, animation=ANIM_REVEAL),
                   cap, now)[:, :, 3].sum()
    dim = _draw(SubtitleStyle(enabled=True, shadow=False, karaoke=False, dim_upcoming=True), cap, now)[:, :, 3].sum()
    assert reveal < full * 0.6 and reveal < dim < full


def test_key_word_colour(qapp):
    cap = _caption("ЭТО", "ЖЕСТЬ")
    style = SubtitleStyle(enabled=True, karaoke=False, shadow=False, emphasis=True, emphasis_color="#00ff00")
    img = _draw(style, cap, cap.start_us + 10_000)
    xs = np.nonzero(_green(img).any(axis=0))[0]
    assert _green(img).sum() > 200 and xs.mean() > 270  # «ЖЕСТЬ» (right) is green, «ЭТО» is not


def test_emoji_places_and_user_choice(qapp):
    cap = _caption("ЭТО", "ЖЕСТЬ")
    rows = lambda img: np.nonzero(img[:, :, 3].max(axis=1))[0]  # noqa: E731
    now = cap.words[1].start_us + 300_000
    plain = rows(_draw(SubtitleStyle(enabled=True, shadow=False), cap, now))
    above = rows(_draw(SubtitleStyle(enabled=True, shadow=False, emoji=True), cap, now))
    below = rows(_draw(SubtitleStyle(enabled=True, shadow=False, emoji=True, emoji_place=EMOJI_BELOW), cap, now))
    assert above[0] < plain[0] - 10 and below[-1] > plain[-1] + 10
    none = _caption("ЭТО", "ЖЕСТЬ", emoji="")
    assert rows(_draw(SubtitleStyle(enabled=True, shadow=False, emoji=True), none, now))[0] >= plain[0] - 2
    chosen = _caption("ПРИВЕТ", emoji="🐐")  # the user's emoji shows even with automatic emoji off
    assert rows(_draw(SubtitleStyle(enabled=True, shadow=False), chosen, chosen.start_us + 1))[0] < plain[0] - 10


def test_style_preview_and_overlay(qapp, tmp_path):
    from core.subtitles.raster import render_overlay, style_preview

    for style in PRESETS.values():
        image = style_preview(style, 150, 64)
        assert image.width() == 150 and not image.isNull()
    cap = _caption("РАЗ", "ДВА", "ЖЕСТЬ")
    out = render_overlay([cap], PRESETS["Karaoke sweep"], 108, 192, tmp_path / "k", duration_us=4 * S)
    lines = out.read_text(encoding="utf-8").splitlines()
    total = sum(float(x.split()[1]) for x in lines if x.startswith("duration"))
    assert len(list(out.parent.glob("cap*.png"))) > 20 and total >= 4.0


def test_panel_new_controls_and_user_styles(qapp):
    from ui.subtitles.subtitles_panel import SubtitlesPanel

    panel = SubtitlesPanel()
    seen, saved = [], []
    panel.settings_changed.connect(seen.append)
    panel.user_presets_changed.connect(saved.append)
    panel.set_settings(type(panel.settings())(SubtitleStyle(enabled=True, emoji=True)))
    panel.karaoke_combo.setCurrentIndex(panel.karaoke_combo.findData(KARAOKE_SWEEP))
    panel.dim_check.setChecked(True)
    panel.emphasis_check.setChecked(True)
    panel.emoji_place_combo.setCurrentIndex(panel.emoji_place_combo.findData(EMOJI_INLINE))
    panel.animation_combo.setCurrentIndex(panel.animation_combo.findData(ANIM_SLIDE))
    style = seen[-1].style
    assert (style.karaoke_mode, style.dim_upcoming, style.emphasis, style.emoji_place, style.animation) == \
        (KARAOKE_SWEEP, True, True, EMOJI_INLINE, ANIM_SLIDE)
    assert panel.preset_list.count() == len(PRESETS)

    panel.save_preset("Мой стиль")
    assert saved[-1]["Мой стиль"]["karaoke_mode"] == KARAOKE_SWEEP
    assert panel.preset_list.count() == len(PRESETS) + 1
    panel.preset_list.itemClicked.emit(panel.preset_item("Clean"))
    assert panel.style().karaoke_mode == KARAOKE_WORD
    panel.preset_list.itemClicked.emit(panel.preset_item("Мой стиль", built_in=False))
    assert panel.style().karaoke_mode == KARAOKE_SWEEP and panel.style().dim_upcoming
    panel.delete_preset("Мой стиль")
    assert saved[-1] == {} and panel.preset_list.count() == len(PRESETS)

    panel.set_user_presets({"A": SubtitleStyle(size=50).to_dict(), "bad": 3})
    assert list(panel.user_presets()) == ["A"]

    cap = _caption("ЭТО", "ЖЕСТЬ")
    panel.set_captions([cap])
    assert "🔥" in panel.list.item(0).text()
    panel.list.setCurrentRow(0)
    assert panel.set_caption_emoji("🐐")
    assert seen[-1].edits.emoji_at(cap.words[0].source_us) == "🐐"
    assert panel.reset_button.isEnabled()
    panel.deleteLater()


def test_config_keeps_user_styles(data_dir):
    from app.config import AppConfig

    config = AppConfig.load(data_dir)
    config.subtitle_presets = {"Мой": SubtitleStyle(size=50).to_dict()}
    config.save()
    assert AppConfig.load(data_dir).subtitle_presets["Мой"]["size"] == 50

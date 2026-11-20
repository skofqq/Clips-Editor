"""Animated captions: pop-in, word jump, box on the spoken word, emoji, presets."""

from __future__ import annotations

import numpy as np
import pytest

from core.subtitles.emoji import emoji_for
from core.subtitles.model import (
    ANIM_NONE,
    ANIM_POP,
    ANIM_WORD,
    PRESETS,
    Caption,
    CaptionWord,
    SubtitleStyle,
)

S = 1_000_000


def _caption(*texts: str, start: float = 1.0) -> Caption:
    words = tuple(CaptionWord(t, int((start + i * 0.5) * S), int((start + i * 0.5 + 0.4) * S), 0)
                  for i, t in enumerate(texts))
    return Caption(words[0].start_us, words[-1].end_us + 300_000, words)


@pytest.mark.parametrize("words, emoji", [
    (["АХАХАХА"], "😂"), (["это", "жёстко"], "🔥"), (["ЖЕСТЬ"], "🔥"), (["он", "умер"], "💀"),
    (["клатч"], "🏆"), (["no", "way"], "😱"), (["деньги"], "💰"), (["привет", "ребята"], ""),
    (["Б***"], ""), (["накрутил"], ""),
])
def test_emoji_cues(words, emoji):
    assert emoji_for(words) == emoji


def test_style_fields_round_trip():
    style = SubtitleStyle(enabled=True, animation=ANIM_WORD, highlight_box=True, emoji=True)
    assert SubtitleStyle.from_dict(style.to_dict()) == style
    assert SubtitleStyle.from_dict({"animation": "spin"}).animation == ANIM_NONE
    assert SubtitleStyle().animation == ANIM_NONE and not SubtitleStyle().emoji  # old projects look the same
    assert all(p.enabled for p in PRESETS.values()) and "Green box" in PRESETS


def test_scales_and_frame_times():
    from core.subtitles.raster import FRAME_US, POP_US, WORD_SCALE, caption_scale, frame_times, word_scale

    cap = _caption("раз", "два", "три")
    pop = SubtitleStyle(enabled=True, animation=ANIM_POP)
    assert caption_scale(pop, cap, cap.start_us) == pytest.approx(0.7)
    assert caption_scale(pop, cap, cap.start_us + POP_US) == 1.0
    assert max(caption_scale(pop, cap, cap.start_us + t) for t in range(0, POP_US, 10_000)) > 1.0  # overshoot
    jump = SubtitleStyle(enabled=True, animation=ANIM_WORD)
    assert word_scale(jump, cap, 1, cap.words[1].start_us + POP_US) == WORD_SCALE
    assert word_scale(pop, cap, 1, cap.words[1].start_us) == 1.0

    still = SubtitleStyle(enabled=True, karaoke=False)
    assert frame_times(cap, still) == [cap.start_us]
    karaoke = SubtitleStyle(enabled=True)
    assert frame_times(cap, karaoke) == [w.start_us for w in cap.words]
    steps = POP_US // FRAME_US + 1
    assert len(frame_times(cap, pop)) == len(cap.words) + steps
    assert len(frame_times(cap, jump)) == len(cap.words) * (1 + steps)
    assert all(cap.start_us <= t < cap.end_us for t in frame_times(cap, jump))


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


def _width(img: np.ndarray) -> int:
    cols = np.nonzero(img[:, :, 3].max(axis=0))[0]
    return int(cols[-1] - cols[0]) if cols.size else 0


def test_pop_in_grows_the_caption(qapp):
    cap = _caption("РАЗ", "ДВА")
    style = SubtitleStyle(enabled=True, animation=ANIM_POP, shadow=False)
    assert _width(_draw(style, cap, cap.start_us)) < _width(_draw(style, cap, cap.start_us + 400_000)) * 0.85


def test_word_box_paints_the_highlight(qapp):
    cap = _caption("РАЗ", "ДВА")
    style = SubtitleStyle(enabled=True, highlight="#00ff00", highlight_box=True, shadow=False)
    img = _draw(style, cap, cap.words[1].start_us + 10_000)
    green = (img[:, :, 1] > 200) & (img[:, :, 0] < 60) & (img[:, :, 2] < 60)
    xs = np.nonzero(green.any(axis=0))[0]
    assert green.sum() > 500 and xs.mean() > 270  # a green box behind the 2nd (right) word


def test_overlay_writes_animation_frames(qapp, tmp_path):
    from core.subtitles.raster import render_overlay

    cap = _caption("РАЗ", "ДВА", "ТРИ")
    plain = render_overlay([cap], SubtitleStyle(enabled=True, karaoke=False), 108, 192, tmp_path / "a")
    jump = render_overlay([cap], SubtitleStyle(enabled=True, animation=ANIM_WORD, emoji=True), 108, 192,
                          tmp_path / "b")
    count = lambda p: len(list(p.parent.glob("cap*.png")))  # noqa: E731
    assert count(plain) == 1 and count(jump) > 3 * 5
    lines = jump.read_text(encoding="utf-8").splitlines()
    total = sum(float(x.split()[1]) for x in lines if x.startswith("duration"))
    assert total >= cap.end_us / S


def test_panel_controls(qapp):
    from ui.subtitles.subtitles_panel import SubtitlesPanel

    panel = SubtitlesPanel()
    seen = []
    panel.settings_changed.connect(seen.append)
    panel.set_settings(type(panel.settings())(SubtitleStyle(enabled=True)))
    panel.animation_combo.setCurrentIndex(panel.animation_combo.findData(ANIM_WORD))
    panel.word_box_check.setChecked(True)
    panel.emoji_check.setChecked(True)
    style = seen[-1].style
    assert (style.animation, style.highlight_box, style.emoji) == (ANIM_WORD, True, True)
    panel.preset_combo.activated.emit(list(PRESETS).index("Purple box"))
    assert panel.style().highlight_box and panel.animation_combo.currentData() == ANIM_POP
    panel.deleteLater()

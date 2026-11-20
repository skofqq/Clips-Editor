"""Draws captions with Qt: the preview and the export use the same code.

The export renders one transparent PNG per caption state (one per word
when the spoken word is highlighted) and an FFmpeg *concat* list with
their durations; the render graph overlays that image stream on the
canvas. Because Qt draws both, the video looks exactly like the preview.
"""

from __future__ import annotations

from pathlib import Path

import math
import os
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QTransform

from core.subtitles.emoji import emoji_for
from core.subtitles.model import (
    ANIM_NONE,
    ANIM_POP,
    ANIM_WORD,
    POSITION_DIVIDER,
    Caption,
    SubtitleStyle,
    speaker_text_color,
    wrap_lines,
)

REFERENCE_H = 1920  # style sizes are given for a 1080×1920 canvas
POP_US = 180_000  # length of the pop-in and word-jump animations
FRAME_US = 40_000  # export draws animations at 25 images per second
WORD_SCALE = 1.12  # the spoken word stays this much bigger in the "word" animation
PNG_QUALITY = 80  # Qt's PNG "quality" is the compression level: 80 is fast and still small
EMOJI_FONTS = ["Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji"]


def _ease_out_back(t: float) -> float:
    """0 → 1 with a small overshoot, the usual "pop"."""
    t = min(1.0, max(0.0, t))
    c1 = 1.70158
    return 1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2


def caption_scale(style: SubtitleStyle, caption: Caption, now_us: int | None) -> float:
    """Size of the whole caption: grows from 70 % to 100 % when it appears."""
    if style.animation == ANIM_NONE or now_us is None:
        return 1.0
    t = (now_us - caption.start_us) / POP_US
    return 1.0 if t >= 1 else 0.7 + 0.3 * _ease_out_back(t)


def word_scale(style: SubtitleStyle, caption: Caption, index: int, now_us: int | None) -> float:
    """Size of the spoken word: jumps up, then stays a little bigger while it is spoken."""
    if style.animation != ANIM_WORD or now_us is None:
        return 1.0
    t = (now_us - caption.words[index].start_us) / POP_US
    if t >= 1:
        return WORD_SCALE
    return 1.0 + (WORD_SCALE - 1.0) * _ease_out_back(t) + 0.1 * math.sin(math.pi * max(0.0, t))


def _readable_on(color: QColor) -> QColor:
    light = 0.299 * color.redF() + 0.587 * color.greenF() + 0.114 * color.blueF()
    return QColor("#111111") if light > 0.62 else QColor("#ffffff")


def _color(value: str, fallback: str = "#ffffff") -> QColor:
    text = (value or "").strip()
    if len(text) == 9 and text.startswith("#"):  # #rrggbbaa
        color = QColor(text[:7])
        color.setAlpha(int(text[7:9], 16))
        return color if color.isValid() else QColor(fallback)
    color = QColor(text)
    return color if color.isValid() else QColor(fallback)


def caption_font(style: SubtitleStyle, scale: float) -> QFont:
    font = QFont(style.font)
    font.setPixelSize(max(6, int(round(style.size * scale))))
    font.setWeight(QFont.Weight.Black if style.bold else QFont.Weight.DemiBold)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


def centre_y(style: SubtitleStyle, canvas: QRectF, divider: float | None) -> float:
    if style.position == POSITION_DIVIDER:
        fraction = divider if divider is not None and 0.05 < divider < 0.95 else 0.72
    else:
        fraction = style.position
    return canvas.top() + canvas.height() * fraction


def draw_caption(painter: QPainter, canvas: QRectF, caption: Caption | None, style: SubtitleStyle,
                 now_us: int | None = None, divider: float | None = None) -> None:
    """Paint ``caption`` on ``canvas`` (the output frame, any size)."""
    if caption is None or not caption.words:
        return
    scale = canvas.height() / REFERENCE_H
    font = caption_font(style, scale)
    words = [w.text for w in caption.words]
    lines = wrap_lines(words, style.max_chars, style.max_lines)
    metrics = QFontMetricsF(font)
    max_width = canvas.width() * 0.9
    widest = max(metrics.horizontalAdvance(" ".join(words[i] for i in line)) for line in lines)
    if widest > max_width:  # shrink to fit instead of running off the frame
        font.setPixelSize(max(6, int(font.pixelSize() * max_width / widest)))
        metrics = QFontMetricsF(font)
    space = metrics.horizontalAdvance(" ")
    line_h = metrics.height() * 1.02
    current = caption.word_at(now_us) if (style.karaoke and now_us is not None) else None
    if current is None and style.animation == ANIM_WORD and now_us is not None:
        current = caption.word_at(now_us)  # the jump shows the spoken word even without a colour
    pop = caption_scale(style, caption, now_us)

    total_h = line_h * len(lines)
    top = centre_y(style, canvas, divider) - total_h / 2
    top = max(canvas.top() + 4, min(top, canvas.bottom() - total_h - 4))
    outline = style.outline_px * scale
    text_color, highlight = _color(style.color), _color(style.highlight, style.color)

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    centre = QPointF(canvas.center().x(), top + total_h / 2)
    if pop != 1.0:  # the whole caption grows around its centre
        painter.translate(centre)
        painter.scale(pop, pop)
        painter.translate(-centre)
    placed: list[tuple[int, QPainterPath]] = []
    boxes: list[QRectF] = []
    for row, line in enumerate(lines):
        width = sum(metrics.horizontalAdvance(words[i]) for i in line) + space * (len(line) - 1)
        x = canvas.center().x() - width / 2
        baseline = top + row * line_h + metrics.ascent()
        boxes.append(QRectF(x, top + row * line_h, width, line_h))
        for i in line:
            path = QPainterPath()
            path.addText(QPointF(x, baseline), font, words[i])
            if i == current:
                grow = word_scale(style, caption, i, now_us)
                if grow != 1.0:
                    c = QPointF(x + metrics.horizontalAdvance(words[i]) / 2, top + row * line_h + line_h / 2)
                    path = QTransform().translate(c.x(), c.y()).scale(grow, grow).translate(-c.x(), -c.y()).map(path)
            placed.append((i, path))
            x += metrics.horizontalAdvance(words[i]) + space

    if style.box:
        pad = 0.35 * metrics.height()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_color(style.box_color, "#000000"))
        for rect in boxes:
            painter.drawRoundedRect(rect.adjusted(-pad, -pad * 0.3, pad, pad * 0.3), pad * 0.6, pad * 0.6)
    word_box = style.highlight_box and current is not None
    if word_box:  # the spoken word on a rounded box of the highlight colour
        rect = dict(placed)[current].boundingRect()
        pad = 0.18 * metrics.height()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_color(style.highlight, style.color))
        painter.drawRoundedRect(rect.adjusted(-pad, -pad * 0.7, pad, pad * 0.7), pad, pad)
    if style.shadow:
        offset = max(1.0, 4 * scale)
        shadow = QColor(0, 0, 0, 110)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow)
        for _i, path in placed:
            painter.drawPath(path.translated(offset, offset * 1.5))
            if outline > 0:
                painter.strokePath(path.translated(offset, offset * 1.5),
                                   QPen(shadow, outline * 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                        Qt.PenJoinStyle.RoundJoin))
    if outline > 0:
        pen = QPen(_color(style.outline, "#000000"), outline * 2, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        for i, path in placed:
            if not (word_box and i == current):  # no outline on the box
                painter.strokePath(path, pen)
    painter.setPen(Qt.PenStyle.NoPen)
    for i, path in placed:
        speaker = caption.words[i].speaker
        own = text_color if speaker <= 0 or not style.speaker_colors else _color(speaker_text_color(style, speaker))
        if i == current and word_box:
            painter.setBrush(_readable_on(highlight))
        elif i == current and style.karaoke:
            painter.setBrush(highlight)
        else:
            painter.setBrush(own)
        painter.drawPath(path)
    if style.emoji:
        _draw_emoji(painter, caption, font, centre, top, metrics)
    painter.restore()


def caption_emoji(caption: Caption) -> str:
    return emoji_for([w.text for w in caption.words if not w.masked])


def _draw_emoji(painter: QPainter, caption: Caption, font: QFont, centre: QPointF, top: float,
                metrics: QFontMetricsF) -> None:
    emoji = caption_emoji(caption)
    if not emoji:
        return
    size = max(8, int(font.pixelSize() * 1.25))
    efont = QFont()
    efont.setFamilies(EMOJI_FONTS)
    efont.setPixelSize(size)
    rect = QRectF(centre.x() - size, top - size * 1.35, size * 2, size * 1.4)
    painter.setFont(efont)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, emoji)


def frame_times(caption: Caption, style: SubtitleStyle) -> list[int]:
    """Output times where the caption's picture changes: its start, every spoken word
    (when words are highlighted or jump) and the steps of each animation."""
    per_word = (style.karaoke and (style.highlight.lower() != style.color.lower() or style.highlight_box)) \
        or style.animation == ANIM_WORD
    keys = {caption.start_us}
    if per_word:
        keys |= {w.start_us for w in caption.words}
    animated = set()
    if style.animation in (ANIM_POP, ANIM_WORD):
        animated.add(caption.start_us)
    if style.animation == ANIM_WORD:
        animated |= {w.start_us for w in caption.words}
    times = set(keys)
    for start in animated:
        times |= {start + step for step in range(FRAME_US, POP_US + FRAME_US, FRAME_US)}
    return sorted(t for t in times if caption.start_us <= t < caption.end_us)


def _states(caption: Caption, style: SubtitleStyle) -> list[tuple[int, int]]:
    """(start, end) intervals with one picture each; the picture is drawn at ``start``."""
    times = frame_times(caption, style)
    return [(t, times[i + 1] if i + 1 < len(times) else caption.end_us) for i, t in enumerate(times)]


def render_overlay(captions: list[Caption], style: SubtitleStyle, width: int, height: int,
                   folder: Path, divider: float | None = None, duration_us: int | None = None,
                   cancel=None, divider_at=None) -> Path | None:
    """Write caption images and ``captions.ffconcat``; None if there is nothing to show.

    ``divider_at(output_us)`` gives the divider when it changes over time (parts
    with their own layout); otherwise ``divider`` is used throughout."""
    if not captions:
        return None
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    canvas = QRectF(0, 0, width, height)
    blank = folder / "blank.png"
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    image.save(str(blank))

    entries: list[tuple[Path, int]] = []
    jobs: list[tuple[Path, Caption, int]] = []
    cursor = 0
    for caption in captions:
        for start, end in _states(caption, style):
            if start > cursor:
                entries.append((blank, start - cursor))
            path = folder / f"cap{len(jobs) + 1:05d}.png"
            jobs.append((path, caption, start))
            entries.append((path, end - max(start, cursor)))
            cursor = max(cursor, end)

    def draw(job: tuple[Path, Caption, int]) -> None:
        if cancel is not None and cancel.is_set():
            return
        path, caption, start = job
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        draw_caption(painter, canvas, caption, style, start, divider_at(start) if divider_at is not None else divider)
        painter.end()
        image.save(str(path), "PNG", PNG_QUALITY)

    # Animated captions need hundreds of images: draw and compress them on several threads.
    workers = min(8, max(1, (os.cpu_count() or 2) - 1))
    if len(jobs) < 8 or workers == 1:
        for job in jobs:
            draw(job)
    else:
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(draw, jobs))
    if cancel is not None and cancel.is_set():
        return None
    tail = max(1_000_000, (duration_us or cursor) - cursor + 1_000_000)
    entries.append((blank, tail))

    lines = ["ffconcat version 1.0"]
    for path, length in entries:
        if length <= 0:
            continue
        lines.append(f"file '{path.name}'")
        lines.append(f"duration {length / 1_000_000:.6f}")
    lines.append(f"file '{blank.name}'")  # the last entry's duration is only honoured if repeated
    out = folder / "captions.ffconcat"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


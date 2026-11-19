"""Draws captions with Qt: the preview and the export use the same code.

The export renders one transparent PNG per caption state (one per word
when the spoken word is highlighted) and an FFmpeg *concat* list with
their durations; the render graph overlays that image stream on the
canvas. Because Qt draws both, the video looks exactly like the preview.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen

from core.subtitles.model import POSITION_DIVIDER, Caption, SubtitleStyle, speaker_text_color, wrap_lines

REFERENCE_H = 1920  # style sizes are given for a 1080×1920 canvas


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

    total_h = line_h * len(lines)
    top = centre_y(style, canvas, divider) - total_h / 2
    top = max(canvas.top() + 4, min(top, canvas.bottom() - total_h - 4))
    outline = style.outline_px * scale
    text_color, highlight = _color(style.color), _color(style.highlight, style.color)

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
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
            placed.append((i, path))
            x += metrics.horizontalAdvance(words[i]) + space

    if style.box:
        pad = 0.35 * metrics.height()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_color(style.box_color, "#000000"))
        for rect in boxes:
            painter.drawRoundedRect(rect.adjusted(-pad, -pad * 0.3, pad, pad * 0.3), pad * 0.6, pad * 0.6)
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
        for _i, path in placed:
            painter.strokePath(path, pen)
    painter.setPen(Qt.PenStyle.NoPen)
    for i, path in placed:
        speaker = caption.words[i].speaker
        own = text_color if speaker <= 0 or not style.speaker_colors else _color(speaker_text_color(style, speaker))
        painter.setBrush(highlight if i == current else own)
        painter.drawPath(path)
    painter.restore()


def _states(caption: Caption, style: SubtitleStyle) -> list[tuple[int, int, int | None]]:
    """(start, end, highlighted word) intervals that make up one caption."""
    if not style.karaoke or style.highlight.lower() == style.color.lower():
        return [(caption.start_us, caption.end_us, None)]
    result = []
    words = caption.words
    for i, word in enumerate(words):
        start = caption.start_us if i == 0 else word.start_us
        end = words[i + 1].start_us if i + 1 < len(words) else caption.end_us
        if end > start:
            result.append((start, end, i))
    return result or [(caption.start_us, caption.end_us, None)]


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
    cursor = 0
    n = 0
    for caption in captions:
        for start, end, word in _states(caption, style):
            if cancel is not None and cancel.is_set():
                return None
            if start > cursor:
                entries.append((blank, start - cursor))
            image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            moment = caption.words[word].start_us if word is not None else None
            draw_caption(painter, canvas, caption, style if word is not None else _no_karaoke(style),
                         moment, divider_at(start) if divider_at is not None else divider)
            painter.end()
            n += 1
            path = folder / f"cap{n:05d}.png"
            image.save(str(path))
            entries.append((path, end - max(start, cursor)))
            cursor = max(cursor, end)
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


def _no_karaoke(style: SubtitleStyle) -> SubtitleStyle:
    from dataclasses import replace

    return replace(style, karaoke=False)

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

from core.subtitles.emoji import emoji_match, is_key_word
from core.subtitles.model import (
    ANIM_NONE,
    ANIM_POP,
    ANIM_REVEAL,
    ANIM_SLIDE,
    ANIM_WORD,
    EMOJI_BELOW,
    EMOJI_INLINE,
    KARAOKE_FILL,
    KARAOKE_SWEEP,
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


def _ease_out_cubic(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def caption_scale(style: SubtitleStyle, caption: Caption, now_us: int | None) -> float:
    """Size of the whole caption: grows from 70 % to 100 % when it appears."""
    if style.animation not in (ANIM_POP, ANIM_WORD) or now_us is None:
        return 1.0
    t = (now_us - caption.start_us) / POP_US
    return 1.0 if t >= 1 else 0.7 + 0.3 * _ease_out_back(t)


def caption_slide(style: SubtitleStyle, caption: Caption, now_us: int | None) -> tuple[float, float]:
    """(how far below its place the caption still is, as a share of a line; opacity) for "slide"."""
    if style.animation != ANIM_SLIDE or now_us is None:
        return 0.0, 1.0
    t = (now_us - caption.start_us) / POP_US
    if t >= 1:
        return 0.0, 1.0
    return 0.6 * (1 - _ease_out_cubic(t)), min(1.0, max(0.0, t * 1.4))


def word_scale(style: SubtitleStyle, caption: Caption, index: int, now_us: int | None) -> float:
    """Size of the spoken word: jumps up, then stays a little bigger while it is spoken."""
    if style.animation != ANIM_WORD or now_us is None:
        return 1.0
    t = (now_us - caption.words[index].start_us) / POP_US
    if t >= 1:
        return WORD_SCALE
    return 1.0 + (WORD_SCALE - 1.0) * _ease_out_back(t) + 0.1 * math.sin(math.pi * max(0.0, t))


def reveal_scale(style: SubtitleStyle, caption: Caption, index: int, now_us: int | None) -> float:
    """"Word by word": 0 before the word is spoken, then it pops from 60 % to full size."""
    if style.animation != ANIM_REVEAL or now_us is None:
        return 1.0
    t = (now_us - caption.words[index].start_us) / POP_US
    if t < 0:
        return 0.0
    return 1.0 if t >= 1 else 0.6 + 0.4 * _ease_out_back(t)


def sweep_fraction(caption: Caption, index: int, now_us: int) -> float:
    """How much of word ``index`` the karaoke colour has run through at ``now_us``."""
    word = caption.words[index]
    length = max(1, word.end_us - word.start_us)
    return min(1.0, max(0.0, (now_us - word.start_us) / length))


def caption_emoji(caption: Caption) -> str:
    return caption_emoji_at(caption)[0]


def caption_emoji_at(caption: Caption) -> tuple[str, int]:
    """(emoji, index of the word it belongs to). The user's choice wins over the words."""
    if caption.emoji is not None:
        return caption.emoji, len(caption.words) - 1
    emoji, index = emoji_match([w.text if not w.masked else "" for w in caption.words])
    return emoji, index


def emoji_scale(style: SubtitleStyle, caption: Caption, now_us: int | None) -> float:
    """The emoji pops in with the caption (or, inline, with its word); 0 = not shown yet."""
    emoji, index = caption_emoji_at(caption)
    if not emoji or now_us is None:
        return 1.0
    appear = caption.words[index].start_us if style.emoji_place == EMOJI_INLINE else caption.start_us
    t = (now_us - appear) / POP_US
    if t < 0:
        return 0.0
    if style.animation == ANIM_NONE or t >= 1:
        return 1.0
    return max(0.0, _ease_out_back(t))


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


def _emoji_font(size: int) -> QFont:
    font = QFont()
    font.setFamilies(EMOJI_FONTS)
    font.setPixelSize(max(6, size))
    return font


def draw_caption(painter: QPainter, canvas: QRectF, caption: Caption | None, style: SubtitleStyle,
                 now_us: int | None = None, divider: float | None = None) -> None:
    """Paint ``caption`` on ``canvas`` (the output frame, any size)."""
    if caption is None or not caption.words:
        return
    scale = canvas.height() / REFERENCE_H
    font = caption_font(style, scale)
    words = [w.text for w in caption.words]
    lines = wrap_lines(words, style.max_chars, style.max_lines)
    emoji, emoji_index = caption_emoji_at(caption) if style.emoji else ("", -1)  # off = no emoji at all
    inline = bool(emoji) and style.emoji_place == EMOJI_INLINE
    metrics = QFontMetricsF(font)

    current = caption.word_at(now_us) if (style.karaoke and now_us is not None) else None
    if current is None and style.animation == ANIM_WORD and now_us is not None:
        current = caption.word_at(now_us)  # the jump shows the spoken word even without a colour
    jump = word_scale(style, caption, current, now_us) if current is not None else 1.0

    def extra(i: int) -> float:  # room after a word for an inline emoji
        return metrics.height() * 1.05 if inline and i == emoji_index else 0.0

    def push(i: int) -> float:  # room on each side of a word that has jumped up, so it never covers the others
        return metrics.horizontalAdvance(words[i]) * (jump - 1) / 2 if i == current and jump > 1 else 0.0

    def line_width(line: list[int]) -> float:
        return sum(metrics.horizontalAdvance(words[i]) + extra(i) + 2 * push(i) for i in line) \
            + metrics.horizontalAdvance(" ") * (len(line) - 1)

    max_width = canvas.width() * 0.9
    widest = max(line_width(line) for line in lines)
    if widest > max_width:  # shrink to fit instead of running off the frame
        font.setPixelSize(max(6, int(font.pixelSize() * max_width / widest)))
        metrics = QFontMetricsF(font)
    space = metrics.horizontalAdvance(" ")
    line_h = metrics.height() * 1.02
    pop = caption_scale(style, caption, now_us)
    slide, opacity = caption_slide(style, caption, now_us)

    total_h = line_h * len(lines)
    top = centre_y(style, canvas, divider) - total_h / 2
    top = max(canvas.top() + 4, min(top, canvas.bottom() - total_h - 4))
    outline = style.outline_px * scale
    text_color, highlight = _color(style.color), _color(style.highlight, style.color)
    emphasis = _color(style.emphasis_color, style.color)

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    if opacity < 1.0:
        painter.setOpacity(opacity)
    if slide:
        painter.translate(0, slide * line_h)
    centre = QPointF(canvas.center().x(), top + total_h / 2)
    if pop != 1.0:  # the whole caption grows around its centre
        painter.translate(centre)
        painter.scale(pop, pop)
        painter.translate(-centre)
    placed: list[tuple[int, QPainterPath]] = []
    boxes: list[QRectF] = []
    emoji_spot: QRectF | None = None
    for row, line in enumerate(lines):
        width = line_width(line)
        x = canvas.center().x() - width / 2
        baseline = top + row * line_h + metrics.ascent()
        boxes.append(QRectF(x, top + row * line_h, width, line_h))
        for i in line:
            x += push(i)
            advance = metrics.horizontalAdvance(words[i])
            path = QPainterPath()
            path.addText(QPointF(x, baseline), font, words[i])
            grow = jump if i == current else 1.0
            grow *= reveal_scale(style, caption, i, now_us)
            if grow != 1.0:
                c = QPointF(x + advance / 2, top + row * line_h + line_h / 2)
                path = QTransform().translate(c.x(), c.y()).scale(grow, grow).translate(-c.x(), -c.y()).map(path)
            if grow > 0.0:
                placed.append((i, path))
            if inline and i == emoji_index:
                emoji_spot = QRectF(x + advance + space * 0.35, top + row * line_h, extra(i), line_h)
            x += advance + extra(i) + push(i) + space

    def spoken(i: int) -> bool:
        """The karaoke colour covers this whole word."""
        if not style.karaoke or current is None:
            return False
        if i == current:
            return style.karaoke_mode != KARAOKE_SWEEP or style.highlight_box
        return i < current and style.karaoke_mode in (KARAOKE_FILL, KARAOKE_SWEEP)

    def alpha(i: int) -> float:
        if style.dim_upcoming and now_us is not None and caption.words[i].start_us > now_us:
            return 0.45
        return 1.0

    if style.box:
        pad = 0.35 * metrics.height()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_color(style.box_color, "#000000"))
        for rect in boxes:
            painter.drawRoundedRect(rect.adjusted(-pad, -pad * 0.3, pad, pad * 0.3), pad * 0.6, pad * 0.6)
    paths = dict(placed)
    word_box = style.highlight_box and current is not None and current in paths
    if word_box:  # the spoken word on a rounded box of the highlight colour
        rect = paths[current].boundingRect()
        pad = 0.18 * metrics.height()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_color(style.highlight, style.color))
        painter.drawRoundedRect(rect.adjusted(-pad, -pad * 0.7, pad, pad * 0.7), pad, pad)
    base_opacity = painter.opacity()
    if style.shadow:
        offset = max(1.0, 4 * scale)
        shadow = QColor(0, 0, 0, 110)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow)
        for i, path in placed:
            painter.setOpacity(base_opacity * alpha(i))
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
                painter.setOpacity(base_opacity * alpha(i))
                painter.strokePath(path, pen)
    painter.setPen(Qt.PenStyle.NoPen)
    for i, path in placed:
        painter.setOpacity(base_opacity * alpha(i))
        word = caption.words[i]
        own = text_color if word.speaker <= 0 or not style.speaker_colors \
            else _color(speaker_text_color(style, word.speaker))
        if style.emphasis and not word.masked and is_key_word(word.text):
            own = emphasis
        if i == current and word_box:
            painter.setBrush(_readable_on(highlight))
        elif spoken(i):
            painter.setBrush(highlight)
        else:
            painter.setBrush(own)
        painter.drawPath(path)
        if i == current and style.karaoke and style.karaoke_mode == KARAOKE_SWEEP and not word_box \
                and now_us is not None:
            rect = path.boundingRect()
            done = sweep_fraction(caption, i, now_us)
            if done > 0:
                painter.save()
                painter.setClipRect(QRectF(rect.left() - 2, rect.top() - 2, rect.width() * done + 2,
                                           rect.height() + 4))
                painter.setBrush(highlight)
                painter.drawPath(path)
                painter.restore()
    painter.setOpacity(base_opacity)
    if emoji:
        _draw_emoji(painter, style, caption, emoji, font, centre, top, total_h, emoji_spot, now_us)
    painter.restore()


def _draw_emoji(painter: QPainter, style: SubtitleStyle, caption: Caption, emoji: str, font: QFont,
                centre: QPointF, top: float, total_h: float, spot: QRectF | None, now_us: int | None) -> None:
    grow = emoji_scale(style, caption, now_us)
    if grow <= 0.0:
        return
    if spot is not None:  # inline: right after its word
        size = max(8, int(font.pixelSize() * 0.95))
        rect = QRectF(spot.center().x() - size, spot.center().y() - size * 0.7, size * 2, size * 1.4)
    else:
        size = max(8, int(font.pixelSize() * 1.25))
        y = top + total_h + size * 0.05 if style.emoji_place == EMOJI_BELOW else top - size * 1.35
        rect = QRectF(centre.x() - size, y, size * 2, size * 1.4)
    painter.save()
    if grow != 1.0:
        c = rect.center()
        painter.translate(c)
        painter.scale(grow, grow)
        painter.translate(-c)
    painter.setFont(_emoji_font(size))
    painter.setPen(QColor("#ffffff"))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, emoji)
    painter.restore()


def frame_times(caption: Caption, style: SubtitleStyle) -> list[int]:
    """Output times where the caption's picture changes: its start, every spoken word
    (when words are highlighted, jump, appear or fade) and the steps of each animation."""
    coloured = style.karaoke and (style.highlight.lower() != style.color.lower() or style.highlight_box)
    per_word = coloured or style.animation in (ANIM_WORD, ANIM_REVEAL) or style.dim_upcoming
    keys = {caption.start_us}
    if per_word:
        keys |= {w.start_us for w in caption.words}
    animated = set()
    if style.animation in (ANIM_POP, ANIM_WORD, ANIM_SLIDE):
        animated.add(caption.start_us)
    if style.animation in (ANIM_WORD, ANIM_REVEAL):
        animated |= {w.start_us for w in caption.words}
    if style.emoji:
        emoji, index = caption_emoji_at(caption)
        if emoji:
            appear = caption.words[index].start_us if style.emoji_place == EMOJI_INLINE else caption.start_us
            keys.add(appear)
            if style.animation != ANIM_NONE:
                animated.add(appear)
    times = set(keys)
    for start in animated:
        times |= {start + step for step in range(FRAME_US, POP_US + FRAME_US, FRAME_US)}
    if coloured and style.karaoke_mode == KARAOKE_SWEEP and not style.highlight_box:
        for word in caption.words:  # the colour runs through the word
            times |= set(range(word.start_us, word.end_us + FRAME_US, FRAME_US))
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



def sample_caption(uppercase: bool = True) -> Caption:
    """Three words for style previews: «это просто жесть»."""
    from core.subtitles.model import CaptionWord

    texts = ("это", "просто", "жесть")
    words = tuple(CaptionWord(t.upper() if uppercase else t, i * 400_000, i * 400_000 + 380_000, i * 400_000)
                  for i, t in enumerate(texts))
    return Caption(0, 1_500_000, words)


def style_preview(style: SubtitleStyle, width: int, height: int, background: str = "#26262c") -> QImage:
    """A small picture of ``style``: the sample caption half way through its middle word."""
    from dataclasses import replace as _replace

    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(background))
    painter = QPainter(image)
    canvas = QRectF(0, 0, width, height)
    # Previews are wide: a virtual canvas tall enough for the text to fill about a quarter of the height.
    look = _replace(style, position=0.5, max_words=3, max_lines=1, max_chars=40)
    caption = sample_caption(style.uppercase)
    font_px = height * 0.26
    scaled = QRectF(0, 0, width, font_px * REFERENCE_H / max(16, style.size))
    scaled.moveCenter(canvas.center())
    if style.emoji:  # leave room for the emoji
        from core.subtitles.model import EMOJI_ABOVE

        shift = {EMOJI_ABOVE: 0.55, EMOJI_BELOW: -0.55}.get(style.emoji_place, 0.0)
        scaled.translate(0, shift * font_px)
    painter.setClipRect(canvas)
    draw_caption(painter, scaled, caption, look, 700_000)
    painter.end()
    return image

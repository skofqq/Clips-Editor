"""Draw a thumbnail: the composed Short frame, optional zoom, shade and a big title.

Pure Qt painting (QImage), safe to run off the GUI thread.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

from core.editor.composition import Composition
from core.thumbnails.model import BOTTOM, LANDSCAPE, MIDDLE, TOP, ThumbnailDesign

MAX_BYTES = 2 * 1024 * 1024  # YouTube refuses thumbnails over 2 MB
FONT = "Segoe UI"


def compose_frame(source: QImage, composition: Composition, src_w: int, src_h: int) -> QImage:
    """The Stacked Layout of one source frame, at the composition's output size."""
    out = QImage(composition.output_w, composition.output_h, QImage.Format.Format_RGB32)
    out.fill(QColor("#000000"))
    if source.isNull():
        return out
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setClipRect(QRectF(0, 0, out.width(), out.height()))
    kx, ky = source.width() / max(src_w, 1), source.height() / max(src_h, 1)
    for placement in composition.layout(src_w, src_h):
        dst, src = placement.dst, placement.src
        painter.drawImage(QRectF(dst.x, dst.y, dst.w, dst.h), source,
                          QRectF(src.x * kx, src.y * ky, src.w * kx, src.h * ky))
    painter.end()
    return out


def _cover(painter: QPainter, image: QImage, target: QRectF, zoom: float = 1.0) -> None:
    """Fill ``target`` with ``image`` (cropped to its shape, centred), zoomed in by ``zoom``."""
    if image.isNull():
        return
    scale = max(target.width() / image.width(), target.height() / image.height()) * zoom
    w, h = target.width() / scale, target.height() / scale
    src = QRectF((image.width() - w) / 2, (image.height() - h) / 2, w, h)
    painter.drawImage(target, image, src)


def wrap(text: str, metrics: QFontMetricsF, width: float, max_lines: int = 3) -> list[str]:
    words = text.split()
    lines: list[str] = []
    for word in words:
        if lines and metrics.horizontalAdvance(lines[-1] + " " + word) <= width:
            lines[-1] += " " + word
        else:
            lines.append(word)
    if len(lines) > max_lines:  # keep it short: the rest joins the last line (and shrinks)
        lines = lines[: max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return lines


def draw_thumbnail(frame: QImage, design: ThumbnailDesign, title: str) -> QImage:
    """The finished thumbnail for ``frame`` (a composed Short frame)."""
    w, h = design.output_size
    image = QImage(w, h, QImage.Format.Format_RGB32)
    image.fill(QColor("#000000"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    canvas = QRectF(0, 0, w, h)
    if design.format == LANDSCAPE and not frame.isNull():
        # Blurred-looking, dark copy behind; the vertical frame in the middle.
        small = frame.scaled(max(1, frame.width() // 24), max(1, frame.height() // 24),
                             Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        _cover(painter, small, canvas)
        painter.fillRect(canvas, QColor(0, 0, 0, 110))
        fw = h * frame.width() / frame.height()
        _cover(painter, frame, QRectF((w - fw) / 2, 0, fw, h), design.zoom)
    else:
        _cover(painter, frame, canvas, design.zoom)

    text = design.shown_text(title)
    if text:
        _draw_text(painter, canvas, text, design)
    painter.end()
    return image


def _draw_text(painter: QPainter, canvas: QRectF, text: str, design: ThumbnailDesign) -> None:
    style = design.text_style
    unit = canvas.width() / 1080 if design.format != LANDSCAPE else canvas.height() / 720 * 0.75
    font = QFont(FONT)
    font.setWeight(QFont.Weight.Black)
    font.setPixelSize(max(8, int(design.size * unit)))
    metrics = QFontMetricsF(font)
    width = canvas.width() * 0.88
    lines = wrap(text, metrics, width)
    widest = max(metrics.horizontalAdvance(line) for line in lines)
    if widest > width:
        font.setPixelSize(max(8, int(font.pixelSize() * width / widest)))
        metrics = QFontMetricsF(font)
    line_h = metrics.height() * 0.98
    total = line_h * len(lines)
    margin = canvas.height() * 0.07
    if design.position == TOP:
        top = canvas.top() + margin
    elif design.position == BOTTOM:
        top = canvas.bottom() - margin - total
    else:
        top = canvas.center().y() - total / 2
    # The Up / down slider, kept so the text stays on the picture.
    top += canvas.height() * design.offset / 100
    top = max(canvas.top() + margin * 0.3, min(canvas.bottom() - margin * 0.3 - total, top))

    if design.shade:
        band = QRectF(canvas.left(), top - margin, canvas.width(), total + 2 * margin)
        gradient = QLinearGradient(band.topLeft(), band.bottomLeft())
        dark, clear = QColor(0, 0, 0, 170), QColor(0, 0, 0, 0)
        if design.position == TOP:
            gradient.setColorAt(0, dark), gradient.setColorAt(0.7, QColor(0, 0, 0, 120)), gradient.setColorAt(1, clear)
            band.setTop(canvas.top())
        elif design.position == BOTTOM:
            gradient.setColorAt(0, clear), gradient.setColorAt(0.3, QColor(0, 0, 0, 120)), gradient.setColorAt(1, dark)
            band.setBottom(canvas.bottom())
        else:
            gradient.setColorAt(0, clear), gradient.setColorAt(0.5, dark), gradient.setColorAt(1, clear)
        painter.fillRect(band, gradient)

    paths = []
    for row, line in enumerate(lines):
        x = canvas.center().x() - metrics.horizontalAdvance(line) / 2
        path = QPainterPath()
        path.addText(QPointF(x, top + row * line_h + metrics.ascent()), font, line)
        paths.append((path, QRectF(x, top + row * line_h, metrics.horizontalAdvance(line), line_h)))

    if style.box:
        pad = 0.25 * metrics.height()
        color = QColor(style.box[:7])
        if len(style.box) == 9:
            color.setAlpha(int(style.box[7:], 16))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        for _path, rect in paths:
            painter.drawRoundedRect(rect.adjusted(-pad, -pad * 0.2, pad, pad * 0.2), pad * 0.5, pad * 0.5)
    shadow = max(2.0, font.pixelSize() * 0.05)
    for path, _rect in paths:  # drop shadow
        painter.fillPath(path.translated(shadow, shadow), QColor(0, 0, 0, 140))
    if style.outline:
        pen = QPen(QColor(style.outline), max(2.0, font.pixelSize() * 0.12), Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        for path, _rect in paths:
            painter.strokePath(path, pen)
    for path, _rect in paths:
        painter.fillPath(path, QColor(style.color))


def jpeg_bytes(image: QImage, limit: int = MAX_BYTES) -> bytes:
    """JPEG data under ``limit`` bytes (quality steps down when needed)."""
    data = b""
    for quality in (92, 85, 78, 70, 60, 50, 40):
        buffer = QByteArray()
        device = QBuffer(buffer)
        device.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(device, "JPG", quality)
        device.close()
        data = bytes(buffer.data())
        if len(data) <= limit:
            break
    return data


def save_thumbnail(image: QImage, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".png":
        if not image.save(str(path), "PNG"):
            raise OSError(f"Cannot write {path}")
    else:
        path.write_bytes(jpeg_bytes(image))
    return path


def _blurred(source: QImage, blurs, at_us: int, src_w: int, src_h: int) -> QImage:
    if not blurs:
        return source
    from core.editor.blur_image import apply_blurs

    return apply_blurs(source, blurs, at_us, src_w, src_h)


def make_thumbnail(frame_png: bytes, composition: Composition, src_w: int, src_h: int,
                   design: ThumbnailDesign, title: str, blurs=(), at_us: int = 0) -> QImage:
    """Source frame (PNG data) → the finished thumbnail (with the blur boxes
    active at ``at_us``, source time)."""
    source = _blurred(QImage.fromData(frame_png), blurs, at_us, src_w, src_h)
    return draw_thumbnail(compose_frame(source, composition, src_w, src_h), design, title)


def candidate_frames(ffmpeg: Path, video: Path, items, src_w: int, src_h: int,
                     height: int = 160, cancel=None, blurs=()) -> list[tuple[int, QImage]]:
    """Small composed frames for picking a cover: ``items`` are (source_us, composition)."""
    from core.thumbnails.frame import grab_frame

    result: list[tuple[int, QImage]] = []
    for us, composition in items:
        if cancel is not None and cancel.is_set():
            break
        try:
            png = grab_frame(ffmpeg, video, us)
        except Exception:  # noqa: BLE001 - a frame that cannot be read is skipped
            continue
        frame = compose_frame(_blurred(QImage.fromData(png), blurs, int(us), src_w, src_h), composition, src_w, src_h)
        result.append((int(us), frame.scaledToHeight(height, Qt.TransformationMode.SmoothTransformation)))
    return result


def candidate_times(output_us: int, to_source, peaks=(), count: int = 6, min_gap_us: int = 1_000_000) -> list[int]:
    """Source times worth offering as covers: the loudest moments first (``peaks``,
    source times), then evenly over the Short (``to_source`` maps output → source)."""
    chosen: list[int] = []
    for us in peaks:
        if len(chosen) >= count // 2:
            break
        if all(abs(us - c) >= min_gap_us for c in chosen):
            chosen.append(int(us))
    for i in range(count):
        if len(chosen) >= count:
            break
        us = int(to_source(int(output_us * (i + 0.5) / count)))
        if all(abs(us - c) >= min_gap_us for c in chosen):
            chosen.append(us)
    return sorted(chosen)

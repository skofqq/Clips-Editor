"""Widget that paints video frames, letterboxed to keep the aspect ratio.

Frames are painted by us (not QVideoWidget) so that Phase 2 can draw the
webcam/gameplay crop rectangles and the 9:16 composition on top.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget


def fit_rect(content: QSize, bounds: QRect) -> QRectF:
    """Largest rectangle with the aspect of ``content`` centred inside ``bounds``."""
    if content.isEmpty() or bounds.isEmpty():
        return QRectF()
    scale = min(bounds.width() / content.width(), bounds.height() / content.height())
    w = content.width() * scale
    h = content.height() * scale
    x = bounds.x() + (bounds.width() - w) / 2
    y = bounds.y() + (bounds.height() - h) / 2
    return QRectF(x, y, w, h)


class VideoCanvas(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._placeholder = "Import a video to start (File → Import or drag and drop)"
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 180)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    def set_image(self, image: QImage | None) -> None:
        self._image = image if image is not None and not image.isNull() else None
        self.update()

    def clear(self, placeholder: str | None = None) -> None:
        if placeholder is not None:
            self._placeholder = placeholder
        self._image = None
        self.update()

    def image_rect(self) -> QRectF:
        """Where the current frame is drawn, in widget coordinates."""
        if self._image is None:
            return QRectF()
        return fit_rect(self._image.size(), self.rect())

    def has_image(self) -> bool:
        return self._image is not None

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(960, 540)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1c1c1c"))
        if self._image is None:
            painter.setPen(QColor("#9d9d9d"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder)
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.image_rect(), self._image)

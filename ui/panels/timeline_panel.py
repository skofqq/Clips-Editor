"""Bottom panel: timeline. Phase 1 shows a simple ruler with the playhead;
the real track editor arrives in Phase 3."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from core.media.probe import format_duration


class TimelinePanel(QWidget):
    seek_requested = Signal(int)  # milliseconds

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._duration_ms = 0
        self._position_ms = 0
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_duration(self, ms: int) -> None:
        self._duration_ms = max(0, int(ms))
        self.update()

    def set_position(self, ms: int) -> None:
        self._position_ms = max(0, int(ms))
        self.update()

    def _x_for(self, ms: int) -> float:
        if not self._duration_ms:
            return 0.0
        return 10 + (self.width() - 20) * ms / self._duration_ms

    def _ms_for(self, x: float) -> int:
        if not self._duration_ms:
            return 0
        ratio = (x - 10) / max(1, self.width() - 20)
        return int(max(0.0, min(1.0, ratio)) * self._duration_ms)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._duration_ms:
            self.seek_requested.emit(self._ms_for(event.position().x()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton and self._duration_ms:
            self.seek_requested.emit(self._ms_for(event.position().x()))

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#18191c"))
        if not self._duration_ms:
            p.setPen(QColor("#6b6d72"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Timeline (tracks arrive in Phase 3)")
            return

        # Ruler ticks roughly every 80 px, rounded to whole seconds.
        width = self.width() - 20
        seconds = self._duration_ms / 1000
        step = max(1, round(seconds / max(1, width / 80)))
        p.setPen(QColor("#6b6d72"))
        t = 0
        while t <= seconds:
            x = self._x_for(int(t * 1000))
            p.drawLine(int(x), 0, int(x), 10)
            p.drawText(int(x) + 3, 20, format_duration(t)[:-4])
            t += step

        # Clip block.
        clip = QRectF(10, 30, width, 32)
        p.fillRect(clip, QColor("#3b2a7a"))
        p.setPen(QColor("#7c4dff"))
        p.drawRect(clip)

        # Playhead.
        x = self._x_for(self._position_ms)
        p.setPen(QPen(QColor("#ff4d6d"), 2))
        p.drawLine(int(x), 0, int(x), self.height())

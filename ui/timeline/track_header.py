"""Track header column on the left of the timeline.

Names each track with an icon, shows the waveform zoom, and lets the user
resize tracks by dragging their lower edge (the same handles also work in
the timeline itself). It reads its layout from :class:`TimelineView`, so the
two always line up.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from ui.theme import icon, palette
from ui.timeline.timeline_view import RULER_H, TimelineView

HEADER_W = 112

_ROWS = {
    "video": ("film", "Video", "Kept parts of the clip. Drag an edge to trim, S splits at the playhead."),
    "audio": ("volume", "Audio", "Waveform. Alt+wheel or the wheel here makes it taller; double-click resets."),
    "mute": ("volume_off", "Mute", "Mute and beep regions. Select a range and press M."),
    "speech": ("speech", "Speech", "Recognised words. Click a word to select it."),
}


class TrackHeader(QWidget):
    def __init__(self, view: TimelineView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view = view
        self._drag: tuple[str, float, int] | None = None
        self.setObjectName("trackHeader")
        self.setFixedWidth(HEADER_W)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        view.tracks_changed.connect(self._on_tracks)
        view.controller.changed.connect(self.update)
        self._on_tracks()

    def _on_tracks(self) -> None:
        self.setMinimumHeight(self.view.tracks.content_h)
        self.update()

    def row_at(self, y: float) -> str | None:
        for name, top, height in self.view.tracks.rows():
            if top <= y <= top + height:
                return name
        return None

    # -------------------------------------------------------------- mouse
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        handle = self.view.tracks.handle_at(event.position().y())
        if event.button() == Qt.MouseButton.LeftButton and handle is not None:
            self._drag = (handle, event.position().y(), getattr(self.view.tracks, f"{handle}_h"))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        y = event.position().y()
        if self._drag is not None:
            name, start_y, start_h = self._drag
            self.view.resize_track(name, start_h + y - start_y)
            return
        handle = self.view.tracks.handle_at(y)
        self.setCursor(Qt.CursorShape.SizeVerCursor if handle else Qt.CursorShape.ArrowCursor)
        row = self.row_at(y)
        self.setToolTip("Drag to resize the track" if handle else (_ROWS[row][2] if row else ""))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag = None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.row_at(event.position().y()) == "audio":
            self.view.set_wave_gain(1.0)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self.row_at(event.position().y()) == "audio":
            delta = event.angleDelta()
            self.view.step_wave_gain((delta.y() or delta.x()) / 120)
            event.accept()
        else:
            event.ignore()

    # -------------------------------------------------------------- paint
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        w = self.width()
        p.fillRect(self.rect(), QColor(palette.BASE_ALT))
        p.fillRect(QRectF(0, 0, w, RULER_H), QColor(palette.BASE))
        view = self.view
        t = view.timeline
        enabled = t is not None
        fps = view.controller.fps
        p.setPen(QColor(palette.TEXT_TERTIARY))
        if enabled and fps:
            p.drawText(QRectF(10, 0, w - 14, RULER_H), Qt.AlignmentFlag.AlignVCenter, f"{float(fps):g} fps")

        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        font = p.font()
        for name, top, height in view.tracks.rows():
            rect = QRectF(4, top, w - 8, height)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(palette.LAYER))
            p.drawRoundedRect(rect, 6, 6)
            glyph, title, _tip = _ROWS[name]
            color = palette.TEXT_SECONDARY if enabled else palette.TEXT_DISABLED
            size = 16 if height >= 20 else 12
            icon_top = top + (min(height, 30) - size) / 2
            pixmap = icon(glyph, color).pixmap(size, size)
            p.drawPixmap(QPointF(12, icon_top), pixmap)
            p.setPen(QColor(color))
            p.setFont(font)
            text_rect = QRectF(12 + size + 7, top, w - size - 30, min(height, 30))
            label = title
            if name == "mute" and t is not None and t.mutes:
                label = f"{title}  {len(t.mutes)}"
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
            if name == "audio" and view.wave_gain != 1.0 and height >= 44:
                small = p.font()
                small.setPointSizeF(max(7.0, font.pointSizeF() - 1))
                p.setFont(small)
                p.setPen(QColor(palette.accent().text))
                p.drawText(QRectF(12, top + 26, w - 24, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           f"Wave ×{view.wave_gain:.1f}")
                p.setFont(font)
            # Resize grip: two short lines centred on the lower edge.
            if name != "mute":
                p.setPen(QPen(QColor(palette.TEXT_DISABLED), 1))
                cx, by = w / 2, top + height - 3
                p.drawLine(QPointF(cx - 7, by - 1.5), QPointF(cx + 7, by - 1.5))
                p.drawLine(QPointF(cx - 7, by + 0.5), QPointF(cx + 7, by + 0.5))
        p.setPen(QColor(palette.DIVIDER))
        p.drawLine(QPointF(w - 0.5, 0), QPointF(w - 0.5, self.height()))

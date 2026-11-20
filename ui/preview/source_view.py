"""Original frame with editable webcam / gameplay crop rectangles."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from core.editor.composition import GAMEPLAY, REGION_NAMES, WEBCAM
from core.editor.geometry import Rect
from ui.editor.composition_controller import MIN_CROP_PX, CompositionController
from ui.preview.video_canvas import VideoCanvas

REGION_COLORS = {WEBCAM: QColor("#ff4d9d"), GAMEPLAY: QColor("#35d0ff")}
REGION_LABELS = {WEBCAM: "Webcam", GAMEPLAY: "Gameplay"}
HANDLE = 9  # handle square size, widget pixels
GRAB = 7  # distance within which an edge/handle is grabbed

# Handle id → which edges move: (left, top, right, bottom)
_HANDLES = {
    "tl": (1, 1, 0, 0), "t": (0, 1, 0, 0), "tr": (0, 1, 1, 0),
    "l": (1, 0, 0, 0), "r": (0, 0, 1, 0),
    "bl": (1, 0, 0, 1), "b": (0, 0, 0, 1), "br": (0, 0, 1, 1),
}
_CURSORS = {
    "tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
    "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
    "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
    "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
    "move": Qt.CursorShape.SizeAllCursor, "draw": Qt.CursorShape.CrossCursor,
}


def resize_rect(start: Rect, handle: str, dx: float, dy: float, bounds: Rect, min_size: float) -> Rect:
    """Move the edges named by ``handle`` by (dx, dy), staying inside ``bounds``."""
    left, top, right, bottom = start.x, start.y, start.right, start.bottom
    move_l, move_t, move_r, move_b = _HANDLES[handle]
    if move_l:
        left = min(max(bounds.x, left + dx), right - min_size)
    if move_r:
        right = max(min(bounds.right, right + dx), left + min_size)
    if move_t:
        top = min(max(bounds.y, top + dy), bottom - min_size)
    if move_b:
        bottom = max(min(bounds.bottom, bottom + dy), top + min_size)
    return Rect(left, top, right - left, bottom - top)


class SourceView(VideoCanvas):
    """Shows the source frame; crops are dragged, resized or redrawn with the mouse.

    * Drag inside a rectangle: move it. Drag a handle: resize it.
    * Shift+drag (or drag outside every rectangle): draw the selected crop anew.
    """

    def __init__(self, controller: CompositionController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setMouseTracking(True)
        self._drag_mode: str | None = None  # handle id, "move" or "draw"
        self._drag_name: str | None = None
        self._drag_start = QPointF()
        self._drag_rect: Rect | None = None
        controller.changed.connect(self.update)
        controller.time_changed.connect(self.update)
        controller.selection_changed.connect(lambda _name: self.update())

    # ---------------------------------------------------------- mapping
    def _frame_rect(self) -> QRectF:
        return self.image_rect()

    def source_to_widget(self, rect: Rect) -> QRectF:
        frame = self._frame_rect()
        sw, sh = self.controller.source_size
        kx, ky = frame.width() / sw, frame.height() / sh
        return QRectF(frame.x() + rect.x * kx, frame.y() + rect.y * ky, rect.w * kx, rect.h * ky)

    def widget_to_source(self, point: QPointF) -> tuple[float, float]:
        frame = self._frame_rect()
        sw, sh = self.controller.source_size
        if frame.isEmpty():
            return 0.0, 0.0
        return (
            (point.x() - frame.x()) * sw / frame.width(),
            (point.y() - frame.y()) * sh / frame.height(),
        )

    def _handle_points(self, r: QRectF) -> dict[str, QPointF]:
        cx, cy = r.center().x(), r.center().y()
        return {
            "tl": r.topLeft(), "t": QPointF(cx, r.top()), "tr": r.topRight(),
            "l": QPointF(r.left(), cy), "r": QPointF(r.right(), cy),
            "bl": r.bottomLeft(), "b": QPointF(cx, r.bottom()), "br": r.bottomRight(),
        }

    def _visible_names(self) -> list[str]:
        """Enabled regions, selected one first (it wins hit tests)."""
        names = [n for n in REGION_NAMES if self.controller.region(n).enabled]
        names.sort(key=lambda n: n != self.controller.selected)
        return names

    def hit_test(self, point: QPointF) -> tuple[str | None, str | None]:
        """Return (region name, mode) under ``point``."""
        if not self.has_image():
            return None, None
        names = self._visible_names()
        # Handles of the selected region take priority.
        if names and names[0] == self.controller.selected:
            rect = self.source_to_widget(self.controller.crop_px(names[0]))
            for handle, pos in self._handle_points(rect).items():
                if abs(point.x() - pos.x()) <= GRAB and abs(point.y() - pos.y()) <= GRAB:
                    return names[0], handle
        inside = [n for n in names if self.source_to_widget(self.controller.crop_px(n)).contains(point)]
        if inside:
            # Prefer the selected region, otherwise the smaller rectangle.
            if self.controller.selected in inside:
                return self.controller.selected, "move"
            inside.sort(key=lambda n: self.controller.crop_px(n).w * self.controller.crop_px(n).h)
            return inside[0], "move"
        if self._frame_rect().contains(point):
            return self.controller.selected, "draw"
        return None, None

    # ----------------------------------------------------------- events
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self.has_image():
            return
        pos = event.position()
        name, mode = self.hit_test(pos)
        if name is None:
            return
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and self._frame_rect().contains(pos):
            name, mode = self.controller.selected, "draw"
        elif mode == "move" and name != self.controller.selected:
            # First click on another rectangle selects it; drag continues as a move.
            self.controller.select(name)
        self._drag_name, self._drag_mode = name, mode
        self._drag_start = pos
        self._drag_rect = self.controller.crop_px(name)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position()
        if self._drag_mode is None:
            _name, mode = self.hit_test(pos)
            self.setCursor(_CURSORS.get(mode, Qt.CursorShape.ArrowCursor))
            return
        sw, sh = self.controller.source_size
        bounds = Rect(0, 0, sw, sh)
        x0, y0 = self.widget_to_source(self._drag_start)
        x1, y1 = self.widget_to_source(pos)
        start = self._drag_rect
        if self._drag_mode == "move":
            rect = Rect(start.x + x1 - x0, start.y + y1 - y0, start.w, start.h).moved_inside(bounds)
        elif self._drag_mode == "draw":
            clip = lambda v, hi: max(0.0, min(float(hi), v))  # noqa: E731
            rect = Rect.from_points(clip(x0, sw), clip(y0, sh), clip(x1, sw), clip(y1, sh))
            if rect.w < MIN_CROP_PX or rect.h < MIN_CROP_PX:
                return  # ignore tiny accidental drags
        else:
            rect = resize_rect(start, self._drag_mode, x1 - x0, y1 - y0, bounds, MIN_CROP_PX)
        self.controller.set_crop_px(self._drag_name, rect)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_mode = None
        self._drag_name = None
        self._drag_rect = None

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if not self.has_image():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # Draw the selected region last so it is on top.
        for name in reversed(self._visible_names()):
            selected = name == self.controller.selected
            rect = self.source_to_widget(self.controller.crop_px(name))
            color = REGION_COLORS[name]
            fill = QColor(color)
            fill.setAlpha(40 if selected else 15)
            painter.fillRect(rect, fill)
            painter.setPen(QPen(color, 2 if selected else 1, Qt.PenStyle.SolidLine if selected else Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)

            crop = self.controller.crop_px(name)
            label = f" {REGION_LABELS[name]}  {round(crop.w)}×{round(crop.h)} "
            metrics = painter.fontMetrics()
            tag = QRectF(rect.left(), rect.top(), metrics.horizontalAdvance(label), metrics.height() + 2)
            painter.fillRect(tag, color)
            painter.setPen(QColor("#101014"))
            painter.drawText(tag, Qt.AlignmentFlag.AlignCenter, label)

            if selected:
                painter.setPen(QPen(QColor("#101014"), 1))
                painter.setBrush(color)
                for pos in self._handle_points(rect).values():
                    painter.drawRect(QRectF(pos.x() - HANDLE / 2, pos.y() - HANDLE / 2, HANDLE, HANDLE))

"""Vertical output preview, painted from the same layout the renderer will use."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from core.editor.composition import MAX_SCALE, MIN_SCALE
from core.editor.geometry import Rect, clamp
from ui.editor.composition_controller import CompositionController
from ui.preview.source_view import REGION_COLORS, REGION_LABELS
from ui.preview.video_canvas import fit_rect

DIVIDER_GRAB = 7
MARGIN = 10


class OutputView(QWidget):
    """Shows the composed 9:16 frame.

    * Drag the line between the regions to move the divider.
    * Drag inside a region to reposition its picture; wheel zooms it.
    * Double-click a region to reset its position and zoom.
    """

    def __init__(self, controller: CompositionController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._image: QImage | None = None
        self._drag: str | None = None  # "divider" or "pan"
        self._drag_name: str | None = None
        self._drag_start = QPointF()
        self._drag_offset = (0.0, 0.0)
        self.setMouseTracking(True)
        self.setMinimumSize(160, 284)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        controller.changed.connect(self.update)
        controller.selection_changed.connect(lambda _name: self.update())

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(320, 568)

    def set_image(self, image: QImage | None) -> None:
        self._image = image if image is not None and not image.isNull() else None
        self.update()

    def clear(self) -> None:
        self._image = None
        self.update()

    # ---------------------------------------------------------- mapping
    def canvas_rect(self) -> QRectF:
        comp = self.controller.composition
        bounds = self.rect().adjusted(MARGIN, MARGIN, -MARGIN, -MARGIN)
        return fit_rect(QSize(comp.output_w, comp.output_h), QRect(bounds))

    def _scale(self) -> float:
        canvas = self.canvas_rect()
        return canvas.width() / self.controller.composition.output_w if canvas.width() else 0.0

    def output_to_widget(self, rect: Rect) -> QRectF:
        canvas, k = self.canvas_rect(), self._scale()
        return QRectF(canvas.x() + rect.x * k, canvas.y() + rect.y * k, rect.w * k, rect.h * k)

    def widget_to_output(self, point: QPointF) -> tuple[float, float]:
        canvas, k = self.canvas_rect(), self._scale()
        if not k:
            return 0.0, 0.0
        return (point.x() - canvas.x()) / k, (point.y() - canvas.y()) / k

    def _divider_y(self) -> float | None:
        """Widget y of the divider, or None when only one region is shown."""
        slots = self.controller.composition.slots()
        if len(slots) < 2:
            return None
        top = slots[self.controller.composition.top_name]
        return self.output_to_widget(top).bottom()

    def region_at(self, point: QPointF) -> str | None:
        x, y = self.widget_to_output(point)
        for name, slot in self.controller.composition.slots().items():
            if slot.contains(x, y):
                return name
        return None

    def _on_divider(self, point: QPointF) -> bool:
        y = self._divider_y()
        canvas = self.canvas_rect()
        return (
            y is not None
            and abs(point.y() - y) <= DIVIDER_GRAB
            and canvas.left() <= point.x() <= canvas.right()
        )

    # ----------------------------------------------------------- events
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        if self._on_divider(pos):
            self._drag = "divider"
            return
        name = self.region_at(pos)
        if name is None:
            return
        self.controller.select(name)
        region = self.controller.region(name)
        self._drag, self._drag_name = "pan", name
        self._drag_start = pos
        self._drag_offset = (region.offset_x, region.offset_y)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position()
        if self._drag is None:
            if self._on_divider(pos):
                self.setCursor(Qt.CursorShape.SplitVCursor)
            elif self.region_at(pos):
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        comp = self.controller.composition
        if self._drag == "divider":
            _x, y = self.widget_to_output(pos)
            self.controller.set_divider(y / comp.output_h)
            return
        # Pan: convert the mouse movement into the -1..1 offset range.
        name = self._drag_name
        slot = comp.slots().get(name)
        content = comp.content_rect(name, *self.controller.source_size)
        k = self._scale()
        if slot is None or content is None or not k:
            return
        dx = (pos.x() - self._drag_start.x()) / k
        dy = (pos.y() - self._drag_start.y()) / k
        free_x, free_y = abs(slot.w - content.w) / 2, abs(slot.h - content.h) / 2
        ox, oy = self._drag_offset
        if free_x > 0.5:
            ox = clamp(ox + dx / free_x, -1.0, 1.0)
        if free_y > 0.5:
            oy = clamp(oy + dy / free_y, -1.0, 1.0)
        self.controller.update_region(name, offset_x=ox, offset_y=oy)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag = None
        self._drag_name = None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        name = self.region_at(event.position())
        if name and not self._on_divider(event.position()):
            self.controller.reset_placement(name)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        name = self.region_at(event.position())
        if name is None:
            return
        self.controller.select(name)
        steps = event.angleDelta().y() / 120
        scale = clamp(self.controller.region(name).scale * (1.1 ** steps), MIN_SCALE, MAX_SCALE)
        self.controller.update_region(name, scale=scale)
        event.accept()

    # ------------------------------------------------------------ paint
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1c1c1c"))
        canvas = self.canvas_rect()
        if canvas.isEmpty():
            return
        painter.fillRect(canvas, QColor("#000000"))

        comp = self.controller.composition
        sw, sh = self.controller.source_size
        placements = comp.layout(sw, sh)
        painter.setClipRect(canvas)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        for placement in placements:
            target = self.output_to_widget(placement.dst)
            if self._image is not None:
                # The frame may be decoded at a different size than probed.
                kx, ky = self._image.width() / sw, self._image.height() / sh
                src = placement.src
                painter.drawImage(target, self._image, QRectF(src.x * kx, src.y * ky, src.w * kx, src.h * ky))
            else:
                fill = QColor(REGION_COLORS[placement.name])
                fill.setAlpha(60)
                painter.fillRect(target, fill)
                painter.setPen(QColor("#ffffff"))
                painter.drawText(target, Qt.AlignmentFlag.AlignCenter, REGION_LABELS[placement.name])

        # Selected slot outline.
        selected = self.controller.selected
        slot = comp.slots().get(selected)
        if slot is not None:
            painter.setPen(QPen(REGION_COLORS[selected], 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.output_to_widget(slot).adjusted(1, 1, -1, -1))

        # Divider handle.
        y = self._divider_y()
        if y is not None:
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.drawLine(QPointF(canvas.left(), y), QPointF(canvas.right(), y))
            grip = QRectF(canvas.center().x() - 18, y - 4, 36, 8)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#ffffff"))
            painter.drawRoundedRect(grip, 4, 4)

        painter.setClipping(False)
        painter.setPen(QPen(QColor("#3b3b3b"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(canvas)

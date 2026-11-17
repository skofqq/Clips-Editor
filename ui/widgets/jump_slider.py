"""A slider that jumps to where you click and is easy to grab.

Qt's default slider moves one page per click on the track; for a seek bar
or a volume control that feels broken. Here a left click anywhere on
the widget's full height (not only on the thin track) puts the handle under
the cursor and starts dragging it right away; sliderPressed / sliderMoved /
sliderReleased fire as usual. The mouse wheel still nudges the value.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSlider, QStyle, QStyleOptionSlider, QWidget


class JumpSlider(QSlider):
    def __init__(self, orientation=Qt.Orientation.Horizontal, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(36)

    def value_at(self, x: float, y: float) -> int:
        """Slider value under a point in widget coordinates."""
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self)
        handle = style.subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self)
        if self.orientation() == Qt.Orientation.Horizontal:
            start, span, pos = groove.x(), groove.width() - handle.width(), x - handle.width() / 2
        else:
            start, span, pos = groove.y(), groove.height() - handle.height(), y - handle.height() / 2
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), int(round(pos - start)), max(1, span), option.upsideDown
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled() or self.maximum() <= self.minimum():
            super().mousePressEvent(event)
            return
        # Anywhere on the widget's full height: grab the thumb and put it under the cursor.
        self._dragging = True
        self.setSliderDown(True)  # emits sliderPressed
        self._move_to(event.position())
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if getattr(self, "_dragging", False):
            self._move_to(event.position())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if getattr(self, "_dragging", False) and event.button() == Qt.MouseButton.LeftButton:
            self._move_to(event.position())
            self._dragging = False
            self.setSliderDown(False)  # emits sliderReleased
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _move_to(self, point) -> None:
        value = self.value_at(point.x(), point.y())
        if value != self.sliderPosition():
            self.setSliderPosition(value)  # emits sliderMoved while the slider is down

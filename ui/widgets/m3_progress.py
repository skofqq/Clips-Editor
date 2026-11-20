"""Material 3 Expressive progress: the wavy linear progress indicator, the
shape-morphing loading indicator and a progress card that combines them.

* :class:`WavyProgressBar` is a drop-in ``QProgressBar``: the done part is a
  moving wave in the accent colour, a small gap, then the flat track with a
  stop dot at the end. The wave flattens out near 0 % and 100 %. With an
  empty range (0, 0) it runs as an indeterminate wave.
* :class:`LoadingIndicator` morphs between soft rounded shapes while it
  turns, on a round tonal container.
* :class:`ProgressCard` puts them together for long jobs: the shape, a big
  percentage, small chips with the details, the wave and a line of text.

Animations run only while the widget is visible.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QVBoxLayout, QWidget

from ui.theme import palette

FRAME_MS = 16


def _ease(t: float) -> float:
    """Expressive spatial spring: fast start, a little overshoot, settle."""
    t = max(0.0, min(1.0, t))
    c1, c3 = 1.2, 2.2
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


class _Animated:
    """A frame timer that runs only while the widget is shown."""

    def _init_animation(self) -> None:
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self.update)

    def _seconds(self) -> float:
        return self._clock.elapsed() / 1000

    def showEvent(self, event) -> None:  # noqa: N802
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)


class WavyProgressBar(_Animated, QProgressBar):
    THICKNESS = 4.0
    AMPLITUDE = 3.0
    WAVELENGTH = 40.0
    SPEED = 0.9  # wavelengths per second
    GAP = 6.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._init_animation()
        self.setTextVisible(False)
        self.setFixedHeight(16)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._shown = 0.0  # eased fraction actually drawn

    def setFixedHeight(self, h: int) -> None:  # noqa: N802 - keep room for the wave
        super().setFixedHeight(max(16, int(h)))

    def fraction(self) -> float:
        span = self.maximum() - self.minimum()
        return 0.0 if span <= 0 else (self.value() - self.minimum()) / span

    def _wave(self, path: QPainterPath, x0: float, x1: float, y: float, amplitude: float, phase: float) -> None:
        step = 2.0
        x = x0
        path.moveTo(x, y + amplitude * math.sin((x / self.WAVELENGTH) * 2 * math.pi - phase))
        while x < x1:
            x = min(x1, x + step)
            path.lineTo(x, y + amplitude * math.sin((x / self.WAVELENGTH) * 2 * math.pi - phase))

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        accent = palette.accent()
        w, y = float(self.width()), self.height() / 2
        r = self.THICKNESS / 2
        left, right = r + 1, w - r - 1
        phase = self._seconds() * self.SPEED * 2 * math.pi
        track = QPen(QColor(accent.container), self.THICKNESS, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        active = QPen(QColor(accent.fill), self.THICKNESS, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        if not self.isEnabled():
            track.setColor(QColor(palette.CONTROL))
            active.setColor(QColor(palette.TEXT_DISABLED))

        if self.maximum() <= self.minimum():  # indeterminate: a wave travelling across
            t = (self._seconds() % 1.6) / 1.6
            head = left + (right - left + 160) * _ease(t) - 80
            a, b = max(left, head - 120), min(right, head)
            p.setPen(track)
            p.drawLine(QPointF(left, y), QPointF(right, y))
            if b > a:
                path = QPainterPath()
                self._wave(path, a, b, y, self.AMPLITUDE, phase)
                p.setPen(active)
                p.strokePath(path, active)
            return

        target = self.fraction()
        self._shown += (target - self._shown) * 0.25 if abs(target - self._shown) > 0.0005 else target - self._shown
        x_end = left + (right - left) * self._shown
        # The wave grows in from 0 % and calms down near 100 %.
        amplitude = self.AMPLITUDE * min(1.0, self._shown / 0.04, (1.0 - self._shown) / 0.04)
        start_track = x_end + self.GAP + (r * 2 if self._shown > 0 else 0)
        if start_track < right:
            p.setPen(track)
            p.drawLine(QPointF(start_track, y), QPointF(right, y))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(accent.fill))
            p.drawEllipse(QPointF(right, y), r * 0.6, r * 0.6)  # stop indicator
        if self._shown > 0:
            path = QPainterPath()
            self._wave(path, left, max(left + 0.5, x_end), y, max(0.0, amplitude), phase)
            p.strokePath(path, active)


# Shapes for the loading indicator: (lobes, depth). Radius r(θ) = 1 + depth·cos(lobes·θ).
SHAPES = ((10, 0.07), (5, 0.13), (8, 0.09), (4, 0.15), (9, 0.08), (2, 0.10), (6, 0.12))
MORPH_S = 0.65
TURN_S = 4.7  # seconds per full turn


class LoadingIndicator(_Animated, QWidget):
    """M3 Expressive loading indicator: a shape that morphs and spins."""

    def __init__(self, size: int = 48, contained: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._init_animation()
        self.contained = contained
        self.setFixedSize(size, size)

    def shape_path(self, seconds: float) -> QPainterPath:
        index = int(seconds / MORPH_S)
        a, b = SHAPES[index % len(SHAPES)], SHAPES[(index + 1) % len(SHAPES)]
        mix = _ease((seconds % MORPH_S) / MORPH_S)
        side = min(self.width(), self.height())
        radius = side * (0.30 if self.contained else 0.42)
        center = QPointF(self.width() / 2, self.height() / 2)
        turn = seconds / TURN_S * 2 * math.pi + mix * math.pi / 3
        path = QPainterPath()
        steps = 96
        for i in range(steps + 1):
            theta = i / steps * 2 * math.pi
            ra = 1 + a[1] * math.cos(a[0] * theta)
            rb = 1 + b[1] * math.cos(b[0] * theta)
            r = radius * (ra + (rb - ra) * mix) * (1 + 0.04 * math.sin(mix * math.pi))
            point = QPointF(center.x() + r * math.cos(theta + turn), center.y() + r * math.sin(theta + turn))
            if i == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        path.closeSubpath()
        return path

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        accent = palette.accent()
        if self.contained:
            p.setBrush(QColor(accent.container))
            p.drawEllipse(QRectF(self.rect()))
        p.setBrush(QColor(accent.on_container if self.contained else accent.fill))
        p.drawPath(self.shape_path(self._seconds()))


class ProgressCard(QFrame):
    """A long job in progress: shape, big percentage, detail chips, the wave and a text line."""

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("progressCard")
        self.indicator = LoadingIndicator(52, parent=self)
        self.percent_label = QLabel("0%", self)
        self.percent_label.setObjectName("progressPercent")
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("progressTitle")
        self.chips = QHBoxLayout()
        self.chips.setSpacing(6)
        self.bar = WavyProgressBar(self)
        self.bar.setRange(0, 1000)
        self.detail_label = QLabel(self)
        self.detail_label.setObjectName("muted")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        head = QVBoxLayout()
        head.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(self.percent_label)
        top.addWidget(self.title_label, 1, Qt.AlignmentFlag.AlignVCenter)
        head.addLayout(top)
        head.addLayout(self.chips)
        row = QHBoxLayout()
        row.setSpacing(14)
        row.addWidget(self.indicator, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(head, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addLayout(row)
        layout.addWidget(self.bar)
        layout.addWidget(self.detail_label)
        self._chip_labels: list[QLabel] = []

    def start(self, title: str, detail: str = "") -> None:
        self.title_label.setText(title)
        self.bar.setValue(0)
        self.bar._shown = 0.0
        self.percent_label.setText("0%")
        self.set_chips([])
        self.detail_label.setText(detail)
        self.detail_label.setVisible(bool(detail))
        self.show()

    def set_chips(self, texts: list[str]) -> None:
        while self.chips.count():
            item = self.chips.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._chip_labels = []
        for text in texts:
            chip = QLabel(text, self)
            chip.setObjectName("progressChip")
            self.chips.addWidget(chip)
            self._chip_labels.append(chip)
        self.chips.addStretch(1)

    def chip_texts(self) -> list[str]:
        return [label.text() for label in self._chip_labels]

    def set_progress(self, fraction: float, message: str = "", detail: str | None = None) -> None:
        """``message`` like "31% · 37 fps · 0:33 left · NVENC": the percentage is shown
        big, the other parts as chips."""
        fraction = max(0.0, min(1.0, float(fraction)))
        self.bar.setValue(int(round(fraction * 1000)))
        self.percent_label.setText(f"{fraction * 100:.0f}%")
        parts = [part.strip() for part in message.split("·") if part.strip()]
        if parts and parts[0].rstrip("%").replace(".", "", 1).isdigit():
            parts = parts[1:]
        if parts != self.chip_texts():
            self.set_chips(parts)
        if detail is not None:
            self.detail_label.setText(detail)
            self.detail_label.setVisible(bool(detail))

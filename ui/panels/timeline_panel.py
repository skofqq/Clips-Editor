"""Bottom panel: edit toolbar, timeline view and horizontal scroll bar."""

from __future__ import annotations

import math

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollBar,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.timeline.model import BEEP, MAX_GAIN_DB, MIN_GAIN_DB, SILENCE
from core.timeline.timebase import US, format_timecode, us_to_frame
from ui.theme import icon, palette
from ui.timeline.timeline_controller import MUTE, TimelineController
from ui.timeline.timeline_view import MAX_PPS, MIN_PPS, TimelineView
from ui.timeline.track_header import HEADER_W, TrackHeader
from ui.widgets.jump_slider import JumpSlider

_SCROLL_UNIT_US = 10_000  # scroll bar resolution: 10 ms
_ZOOM_STEPS = 1000  # zoom slider resolution (logarithmic)


def pps_to_slider(pps: float) -> int:
    pps = max(MIN_PPS, min(MAX_PPS, pps))
    return round(_ZOOM_STEPS * math.log(pps / MIN_PPS) / math.log(MAX_PPS / MIN_PPS))


def slider_to_pps(value: int) -> float:
    return MIN_PPS * (MAX_PPS / MIN_PPS) ** (value / _ZOOM_STEPS)


def _separator(parent: QWidget) -> QFrame:
    line = QFrame(parent)
    line.setFixedWidth(1)
    line.setStyleSheet(f"background: {palette.FLYOUT_STROKE}; margin: 6px 4px;")
    return line


def _label(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName("muted")
    return label


class TimelinePanel(QWidget):
    def __init__(self, controller: TimelineController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._updating = False

        self.view = TimelineView(controller, self)
        self.header = TrackHeader(self.view, self)
        self.scrollbar = QScrollBar(Qt.Orientation.Horizontal, self)

        self.toolbar = QHBoxLayout()
        self.toolbar.setContentsMargins(8, 6, 8, 6)
        self.toolbar.setSpacing(5)
        self._action_slot = QHBoxLayout()  # filled by add_actions()
        self._action_slot.setSpacing(4)
        self.toolbar.addLayout(self._action_slot)

        self.mode_combo = QComboBox(self)
        self.mode_combo.addItem("Silence", SILENCE)
        self.mode_combo.addItem("Beep", BEEP)
        self.mode_combo.setToolTip("Mode of the selected Mute Region (and of new ones)")

        self.gain_spin = QDoubleSpinBox(self)
        self.gain_spin.setRange(MIN_GAIN_DB, MAX_GAIN_DB)
        self.gain_spin.setDecimals(1)
        self.gain_spin.setSingleStep(0.5)
        self.gain_spin.setSuffix(" dB")
        self.gain_spin.setFixedWidth(92)
        self.gain_spin.setToolTip("Overall volume. Boost above 0 dB is applied on export; the preview cannot exceed 100%.")
        self.fade_in_spin = self._fade_spin("Fade In length")
        self.fade_out_spin = self._fade_spin("Fade Out length")

        self.zoom_out_button = self._tool("zoom_out", "Zoom out  (−, Ctrl+wheel)")
        self.zoom_in_button = self._tool("zoom_in", "Zoom in  (+, Ctrl+wheel)")
        self.zoom_fit_button = self._tool("zoom_fit", "Show the whole clip  (0)")
        self.zoom_selection_button = self._tool("zoom_selection", "Zoom to the selection  (Z)")
        self.zoom_slider = JumpSlider(Qt.Orientation.Horizontal, self)
        self.zoom_slider.setObjectName("zoomSlider")
        self.zoom_slider.setRange(0, _ZOOM_STEPS)
        self.zoom_slider.setFixedWidth(120)
        self.zoom_slider.setSingleStep(40)
        self.zoom_slider.setPageStep(120)
        self.zoom_slider.setToolTip("Timeline zoom. At the far right every frame is visible.")
        self.info_label = QLabel(self)
        self.info_label.setObjectName("timecode")
        # Let the window shrink: the info text gives way before the buttons do.
        self.info_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        for widget in (
            _separator(self), _label("Mode", self), self.mode_combo,
            _separator(self), _label("Gain", self), self.gain_spin,
            _label("Fade in", self), self.fade_in_spin,
            _label("out", self), self.fade_out_spin,
        ):
            self.toolbar.addWidget(widget)
        self.toolbar.addWidget(self.info_label, 1)
        for widget in (_separator(self), self.zoom_out_button, self.zoom_slider, self.zoom_in_button,
                       self.zoom_fit_button, self.zoom_selection_button):
            self.toolbar.addWidget(widget)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(self.toolbar)
        tracks = QHBoxLayout()
        tracks.setContentsMargins(0, 0, 0, 0)
        tracks.setSpacing(0)
        tracks.addWidget(self.header)
        tracks.addWidget(self.view, 1)
        layout.addLayout(tracks, 1)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(HEADER_W, 0, 0, 0)
        bottom.addWidget(self.scrollbar)
        layout.addLayout(bottom)

        self.zoom_in_button.clicked.connect(self.view.zoom_in)
        self.zoom_out_button.clicked.connect(self.view.zoom_out)
        self.zoom_fit_button.clicked.connect(self.view.zoom_fit)
        self.zoom_selection_button.clicked.connect(self.view.zoom_selection)
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider)
        self.scrollbar.valueChanged.connect(self._on_scrollbar)
        self.view.view_changed.connect(self._sync_scrollbar)
        self.mode_combo.activated.connect(lambda _i: controller.set_mute_mode(self.mode_combo.currentData()))
        self.gain_spin.valueChanged.connect(self._on_gain)
        self.fade_in_spin.valueChanged.connect(self._on_fades)
        self.fade_out_spin.valueChanged.connect(self._on_fades)
        controller.changed.connect(self.refresh)
        controller.selection_changed.connect(self.refresh)
        controller.playhead_changed.connect(lambda _us: self._update_info())
        self.refresh()

    # ------------------------------------------------------------ helpers
    def _tool(self, glyph: str, tip: str) -> QToolButton:
        button = QToolButton(self)
        button.setIcon(icon(glyph))
        button.setIconSize(QSize(18, 18))
        button.setToolTip(tip)
        button.setAccessibleName(tip.split("  (")[0])
        return button

    def _fade_spin(self, tip: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setRange(0.0, 30.0)
        spin.setDecimals(2)
        spin.setSingleStep(0.1)
        spin.setSuffix(" s")
        spin.setToolTip(tip)
        spin.setFixedWidth(86)
        return spin

    def add_actions(self, actions: list[QAction | None]) -> None:
        """Show edit actions as toolbar buttons (None inserts a separator)."""
        for action in actions:
            if action is None:
                self._action_slot.addWidget(_separator(self))
                continue
            button = QToolButton(self)
            button.setDefaultAction(action)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setIconSize(QSize(18, 18))
            self._action_slot.addWidget(button)

    # ------------------------------------------------------------- scroll
    def _on_zoom_slider(self, value: int) -> None:
        if self._updating:
            return
        view = self.view
        anchor = view.us_to_x(self.controller.playhead_us)
        view.set_zoom(slider_to_pps(value), anchor if 0 <= anchor <= view.width() else None)

    def _sync_scrollbar(self) -> None:
        self._updating, was = True, self._updating
        try:
            self.zoom_slider.setValue(pps_to_slider(self.view.pixels_per_second))
        finally:
            self._updating = was
        t = self.controller.timeline
        self.scrollbar.blockSignals(True)
        if t is None:
            self.scrollbar.setRange(0, 0)
        else:
            visible = self.view.visible_us()
            maximum = max(0, t.duration_us - visible + int(20 * US / self.view.pixels_per_second))
            self.scrollbar.setRange(0, maximum // _SCROLL_UNIT_US)
            self.scrollbar.setPageStep(max(1, visible // _SCROLL_UNIT_US))
            self.scrollbar.setSingleStep(max(1, visible // _SCROLL_UNIT_US // 20))
            self.scrollbar.setValue(self.view.scroll_us // _SCROLL_UNIT_US)
        self.scrollbar.blockSignals(False)

    def _on_scrollbar(self, value: int) -> None:
        self.view.set_scroll_us(value * _SCROLL_UNIT_US)

    # ----------------------------------------------------- model ↔ widgets
    def refresh(self) -> None:
        t = self.controller.timeline
        self._updating = True
        try:
            enabled = t is not None
            for widget in (self.gain_spin, self.fade_in_spin, self.fade_out_spin, self.mode_combo,
                           self.zoom_in_button, self.zoom_out_button, self.zoom_fit_button, self.zoom_slider):
                widget.setEnabled(enabled)
            self.zoom_selection_button.setEnabled(self.view.selection_bounds() is not None)
            if t is not None:
                self.gain_spin.setValue(t.gain_db)
                self.fade_in_spin.setValue(t.fade_in_us / US)
                self.fade_out_spin.setValue(t.fade_out_us / US)
            self.mode_combo.setCurrentIndex(self.mode_combo.findData(self.controller.selected_mute_mode()))
        finally:
            self._updating = False
        self._sync_scrollbar()
        self._update_info()

    def _update_info(self) -> None:
        t = self.controller.timeline
        if t is None:
            self.info_label.setText("")
            return
        playhead = format_timecode(self.controller.playhead_us)
        fps = self.controller.fps
        if fps:
            playhead += f"  frame {us_to_frame(self.controller.playhead_us, fps)}"
        parts = [playhead, f"Output {format_timecode(t.output_us)}"]
        if self.controller.range:
            start, end = self.controller.range
            parts.append(f"Selection {format_timecode(end - start)}")
        elif self.controller.selected and self.controller.selected[0] == MUTE:
            region = t.mute(self.controller.selected[1])
            if region:
                parts.append(f"Mute {format_timecode(region.length_us)}")
        self.info_label.setText("   ·   ".join(parts))

    def _on_gain(self, value: float) -> None:
        if not self._updating:
            self.controller.set_gain_db(value)

    def _on_fades(self) -> None:
        if not self._updating:
            self.controller.set_fades(
                int(round(self.fade_in_spin.value() * US)), int(round(self.fade_out_spin.value() * US))
            )

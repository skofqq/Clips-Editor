"""Thumbnail card on the Data page: pick a frame, write a big title, style it.

The editor only edits a :class:`ThumbnailDesign`; the main window grabs the
frame (off the GUI thread) and hands back the finished picture to show.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.thumbnails.model import (
    FORMAT_TEXT,
    MAX_OFFSET,
    MAX_SIZE,
    MAX_ZOOM,
    MIN_SIZE,
    MIN_ZOOM,
    POSITION_TEXT,
    STYLES,
    VERTICAL,
    ThumbnailDesign,
    clip_title,
)
from ui.widgets.card import Card

PREVIEW_W, PREVIEW_H = 216, 384


def _format_time(us: int) -> str:
    seconds = us / 1e6
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


class ThumbnailEditor(Card):
    design_changed = Signal(object)  # ThumbnailDesign
    frame_requested = Signal()  # use the frame under the playhead
    frame_chosen = Signal(int)  # a suggested frame was clicked (source µs)
    scrub_moved = Signal(int)  # the "from the video" slider settled (output µs)
    save_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Thumbnail", "The cover of the Short: a frame of the clip with a big title. "
                         "Set on YouTube after the upload; you can also save it as an image.", parent)
        self._design = ThumbnailDesign()
        self._title = ""
        self._updating = False

        self.preview = QLabel(self)
        self.preview.setObjectName("thumbnailPreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedSize(PREVIEW_W, PREVIEW_H)
        self.preview.setText("No clip")

        self.enabled_check = QCheckBox("Use this thumbnail", self)
        self.frame_button = QPushButton("Use the frame at the playhead", self)
        self.frame_button.setToolTip("Go to the Edit page, put the playhead on a good moment, then press this")
        self.frame_label = QLabel(self)
        self.frame_label.setObjectName("muted")
        # Suggested frames (loudest moments, then evenly over the Short).
        self.frames_row = QHBoxLayout()
        self.frames_row.setSpacing(6)
        self.frame_buttons: list[QToolButton] = []
        self.frames_hint = QLabel("Looking for good frames…", self)
        self.frames_hint.setObjectName("muted")
        self.frames_row.addWidget(self.frames_hint)
        self.frames_row.addStretch(1)
        # Any frame of the Short.
        self.scrub = QSlider(Qt.Orientation.Horizontal, self)
        self.scrub.setRange(0, 0)
        self.scrub.setToolTip("Drag through the Short to pick any frame")
        self.scrub_label = QLabel("0:00.0", self)
        self.scrub_label.setMinimumWidth(52)
        self._scrub_timer = QTimer(self)
        self._scrub_timer.setSingleShot(True)
        self._scrub_timer.setInterval(220)
        self._scrub_timer.timeout.connect(lambda: self.scrub_moved.emit(self.scrub.value() * 1000))
        self.text_edit = QLineEdit(self)
        self.text_edit.setPlaceholderText("The clip title")
        self.style_combo = QComboBox(self)
        for key, style in STYLES.items():
            self.style_combo.addItem(style.name, key)
        self.position_combo = QComboBox(self)
        for key, text in POSITION_TEXT.items():
            self.position_combo.addItem(text, key)
        self.offset_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.offset_slider.setRange(-MAX_OFFSET, MAX_OFFSET)
        self.offset_slider.setToolTip("Left moves the text up, right moves it down. Double-click: back to the middle.")
        self.offset_slider.mouseDoubleClickEvent = lambda _e: self.offset_slider.setValue(0)
        self.offset_label = QLabel(self)
        self.offset_label.setObjectName("muted")
        self.offset_label.setMinimumWidth(44)
        self.size_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.size_slider.setRange(MIN_SIZE, MAX_SIZE)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.zoom_slider.setRange(round(MIN_ZOOM * 100), round(MAX_ZOOM * 100))
        self.uppercase_check = QCheckBox("CAPITALS", self)
        self.shade_check = QCheckBox("Darken behind the text", self)
        self.format_combo = QComboBox(self)
        for key, text in FORMAT_TEXT.items():
            self.format_combo.addItem(text, key)
        self.save_button = QPushButton("Save image…", self)

        frame_row = QHBoxLayout()
        frame_row.addWidget(self.frame_button)
        frame_row.addWidget(self.frame_label, 1)
        checks = QHBoxLayout()
        checks.addWidget(self.uppercase_check)
        checks.addWidget(self.shade_check)
        checks.addStretch(1)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        scrub_row = QHBoxLayout()
        scrub_row.addWidget(self.scrub, 1)
        scrub_row.addWidget(self.scrub_label)
        form.addRow("Frame", frame_row)
        form.addRow("Suggested", self.frames_row)
        form.addRow("From video", scrub_row)
        form.addRow("Text", self.text_edit)
        form.addRow("Style", self.style_combo)
        form.addRow("Position", self.position_combo)
        offset_row = QHBoxLayout()
        offset_row.addWidget(self.offset_slider, 1)
        offset_row.addWidget(self.offset_label)
        form.addRow("Up / down", offset_row)
        form.addRow("Text size", self.size_slider)
        form.addRow("Zoom", self.zoom_slider)
        form.addRow("", checks)
        form.addRow("Format", self.format_combo)
        save_row = QHBoxLayout()
        save_row.addWidget(self.save_button)
        save_row.addStretch(1)
        form.addRow("", save_row)
        controls = QVBoxLayout()
        controls.addWidget(self.enabled_check)
        controls.addLayout(form)
        controls.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(controls, 1)
        self.body.addLayout(row)

        self.enabled_check.toggled.connect(lambda v: self._edit(enabled=v))
        self.text_edit.textEdited.connect(lambda v: self._edit(text=v))
        self.style_combo.currentIndexChanged.connect(lambda _i: self._edit(style=self.style_combo.currentData()))
        self.position_combo.currentIndexChanged.connect(
            lambda _i: self._edit(position=self.position_combo.currentData()))
        self.size_slider.valueChanged.connect(lambda v: self._edit(size=v))
        self.offset_slider.valueChanged.connect(lambda v: (self._show_offset(v), self._edit(offset=v)))
        self.zoom_slider.valueChanged.connect(lambda v: self._edit(zoom=v / 100))
        self.uppercase_check.toggled.connect(lambda v: self._edit(uppercase=v))
        self.shade_check.toggled.connect(lambda v: self._edit(shade=v))
        self.format_combo.currentIndexChanged.connect(lambda _i: self._edit(format=self.format_combo.currentData()))
        self.frame_button.clicked.connect(self.frame_requested)
        self.scrub.valueChanged.connect(self._on_scrub)
        self.save_button.clicked.connect(self.save_requested)
        self.set_design(self._design)

    # ------------------------------------------------------------ state
    def design(self) -> ThumbnailDesign:
        return self._design

    def set_design(self, design: ThumbnailDesign) -> None:
        self._design = design
        self._updating = True
        try:
            self.enabled_check.setChecked(design.enabled)
            if self.text_edit.text() != design.text:
                self.text_edit.setText(design.text)
            for combo, value in ((self.style_combo, design.style), (self.position_combo, design.position),
                                 (self.format_combo, design.format)):
                combo.setCurrentIndex(max(0, combo.findData(value)))
            self.size_slider.setValue(design.size)
            self.offset_slider.setValue(design.offset)
            self._show_offset(design.offset)
            self.zoom_slider.setValue(round(design.zoom * 100))
            self.uppercase_check.setChecked(design.uppercase)
            self.shade_check.setChecked(design.shade)
            self.frame_label.setText(
                f"Frame at {_format_time(design.source_us)} of the source" if design.source_us is not None
                else "A quarter into the clip")
            for widget in (self.frame_button, self.scrub, *self.frame_buttons, self.text_edit, self.style_combo, self.position_combo,
                           self.offset_slider, self.size_slider, self.zoom_slider, self.uppercase_check, self.shade_check,
                           self.format_combo, self.save_button):
                widget.setEnabled(design.enabled)
        finally:
            self._updating = False

    def _show_offset(self, value: int) -> None:
        self.offset_label.setText("0" if value == 0 else (f"↑ {-value}%" if value < 0 else f"↓ {value}%"))

    # ------------------------------------------------------------ frames
    def set_candidates(self, frames: list[tuple[int, QImage]], selected_us: int | None = None) -> None:
        """Suggested frames as clickable pictures: (source µs, small image)."""
        for button in self.frame_buttons:
            self.frames_row.removeWidget(button)
            button.deleteLater()
        self.frame_buttons = []
        self.frames_hint.setVisible(not frames)
        if not frames:
            self.frames_hint.setText("No frames yet")
        for index, (us, image) in enumerate(frames):
            button = QToolButton(self)
            button.setObjectName("frameChoice")
            button.setCheckable(True)
            button.setAutoRaise(True)
            pixmap = QPixmap.fromImage(image)
            button.setIcon(QIcon(pixmap))
            button.setIconSize(QSize(pixmap.width(), pixmap.height()).scaled(
                QSize(64, 114), Qt.AspectRatioMode.KeepAspectRatio))
            button.setToolTip(f"Frame at {_format_time(us)} of the source")
            button.setProperty("source_us", us)
            button.setEnabled(self._design.enabled)
            button.clicked.connect(lambda _c=False, t=us: self.frame_chosen.emit(t))
            self.frames_row.insertWidget(index, button)
            self.frame_buttons.append(button)
        self.mark_candidate(selected_us)

    def mark_candidate(self, source_us: int | None) -> None:
        for button in self.frame_buttons:
            button.setChecked(source_us is not None and button.property("source_us") == source_us)

    def set_scrub(self, length_us: int, position_us: int) -> None:
        """The slider over the Short (output time), placed at the chosen frame."""
        self.scrub.blockSignals(True)
        self.scrub.setRange(0, max(0, length_us // 1000))
        moving = self._scrub_timer.isActive() or self.scrub.isSliderDown()
        if not moving:  # never pull the slider from under the user
            self.scrub.setValue(max(0, position_us // 1000))
            self.scrub_label.setText(_format_time(position_us))
        self.scrub.blockSignals(False)

    def _on_scrub(self, value_ms: int) -> None:
        self.scrub_label.setText(_format_time(value_ms * 1000))
        self._scrub_timer.start()  # grab the frame when the slider rests

    def set_title(self, title: str) -> None:
        """The clip's YouTube title: its first part is the default text."""
        self._title = title
        self.text_edit.setPlaceholderText(clip_title(title) or "The clip title")

    @property
    def title(self) -> str:
        return self._title

    def set_image(self, image: QImage | None, message: str = "") -> None:
        if image is None or image.isNull():
            self.preview.setPixmap(QPixmap())
            self.preview.setText(message or "No clip")
            return
        w, h = (PREVIEW_W, PREVIEW_H) if self._design.format == VERTICAL else (PREVIEW_W, PREVIEW_W * 9 // 16)
        pixmap = QPixmap.fromImage(image).scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio,
                                                 Qt.TransformationMode.SmoothTransformation)
        self.preview.setPixmap(pixmap)

    def _edit(self, **changes) -> None:
        if self._updating:
            return
        design = replace(self._design, **changes).clean()
        if design != self._design:
            self.set_design(design)
            self.design_changed.emit(design)

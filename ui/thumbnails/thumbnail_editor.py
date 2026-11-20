"""Thumbnail card on the Data page: pick a frame, write a big title, style it.

The editor only edits a :class:`ThumbnailDesign`; the main window grabs the
frame (off the GUI thread) and hands back the finished picture to show.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from core.thumbnails.model import (
    FORMAT_TEXT,
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
        self.text_edit = QLineEdit(self)
        self.text_edit.setPlaceholderText("The clip title")
        self.style_combo = QComboBox(self)
        for key, style in STYLES.items():
            self.style_combo.addItem(style.name, key)
        self.position_combo = QComboBox(self)
        for key, text in POSITION_TEXT.items():
            self.position_combo.addItem(text, key)
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
        form.addRow("Frame", frame_row)
        form.addRow("Text", self.text_edit)
        form.addRow("Style", self.style_combo)
        form.addRow("Position", self.position_combo)
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
        self.zoom_slider.valueChanged.connect(lambda v: self._edit(zoom=v / 100))
        self.uppercase_check.toggled.connect(lambda v: self._edit(uppercase=v))
        self.shade_check.toggled.connect(lambda v: self._edit(shade=v))
        self.format_combo.currentIndexChanged.connect(lambda _i: self._edit(format=self.format_combo.currentData()))
        self.frame_button.clicked.connect(self.frame_requested)
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
            self.zoom_slider.setValue(round(design.zoom * 100))
            self.uppercase_check.setChecked(design.uppercase)
            self.shade_check.setChecked(design.shade)
            self.frame_label.setText(
                f"Frame at {_format_time(design.source_us)} of the source" if design.source_us is not None
                else "A quarter into the clip")
            for widget in (self.frame_button, self.text_edit, self.style_combo, self.position_combo,
                           self.size_slider, self.zoom_slider, self.uppercase_check, self.shade_check,
                           self.format_combo, self.save_button):
                widget.setEnabled(design.enabled)
        finally:
            self._updating = False

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

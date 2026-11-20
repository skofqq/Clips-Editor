"""Properties for the Stacked Layout: presets, regions, divider."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.editor.composition import (
    GAMEPLAY,
    MAX_DIVIDER,
    MAX_SCALE,
    MIN_DIVIDER,
    MIN_SCALE,
    WEBCAM,
    FitMode,
)
from core.editor.geometry import Rect
from core.editor.presets import PresetStore
from ui.editor.composition_controller import MIN_CROP_PX, CompositionController
from ui.widgets.card import Card


def _pair(first: str, first_field: QWidget, second: str, second_field: QWidget) -> QHBoxLayout:
    """Two short labelled fields on one row (X and Y, width and height)."""
    row = QHBoxLayout()
    row.setSpacing(8)
    for text, field in ((first, first_field), (second, second_field)):
        label = QLabel(text)
        label.setObjectName("muted")
        label.setFixedWidth(14)
        row.addWidget(label)
        row.addWidget(field, 1)
    return row


class CompositionPanel(QWidget):
    apply_all_requested = Signal()  # use the shown layout for every part of the clip
    autoframe_requested = Signal(bool)  # find the face in the webcam; True = follow it
    autoframe_cancel_requested = Signal()

    def __init__(
        self, controller: CompositionController, presets: PresetStore, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.presets = presets
        self._updating = False

        # ---- presets
        self.preset_combo = QComboBox(self)
        self.preset_combo.setPlaceholderText("Choose a preset…")
        self.preset_combo.setMinimumContentsLength(8)
        self.preset_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.save_preset_button = QPushButton("Save as…", self)
        self.delete_preset_button = QPushButton("Delete", self)
        preset_buttons = QHBoxLayout()
        preset_buttons.addWidget(self.save_preset_button)
        preset_buttons.addWidget(self.delete_preset_button)
        preset_row = QVBoxLayout()
        preset_row.addWidget(self.preset_combo)
        preset_row.addLayout(preset_buttons)

        # ---- region selector
        self.webcam_button = QPushButton("Webcam", self)
        self.gameplay_button = QPushButton("Gameplay", self)
        self._region_group = QButtonGroup(self)
        self._region_group.setExclusive(True)
        for button, name in ((self.webcam_button, WEBCAM), (self.gameplay_button, GAMEPLAY)):
            button.setCheckable(True)
            button.setProperty("region", name)
            self._region_group.addButton(button)
        region_row = QHBoxLayout()
        region_row.addWidget(self.webcam_button)
        region_row.addWidget(self.gameplay_button)

        self.enabled_check = QCheckBox("Show this region", self)

        # ---- crop (source pixels)
        self.crop_x = self._spin()
        self.crop_y = self._spin()
        self.crop_w = self._spin(minimum=MIN_CROP_PX)
        self.crop_h = self._spin(minimum=MIN_CROP_PX)

        # ---- placement inside the slot
        self.fit_combo = QComboBox(self)
        self.fit_combo.addItem("Fill (crop to fit)", FitMode.FILL.value)
        self.fit_combo.addItem("Fit (show all)", FitMode.FIT.value)
        self.scale_spin = QDoubleSpinBox(self)
        self.scale_spin.setRange(MIN_SCALE * 100, MAX_SCALE * 100)
        self.scale_spin.setSuffix(" %")
        self.scale_spin.setDecimals(0)
        self.scale_spin.setSingleStep(5)
        self.offset_x = self._offset_spin()
        self.offset_y = self._offset_spin()
        self.match_button = QPushButton("Match slot aspect", self)
        self.match_button.setToolTip("Resize the crop so it fills its slot without cutting anything")
        self.reset_placement_button = QPushButton("Reset position", self)

        # ---- layout
        self.divider_spin = QDoubleSpinBox(self)
        self.divider_spin.setRange(MIN_DIVIDER * 100, MAX_DIVIDER * 100)
        self.divider_spin.setSuffix(" %")
        self.divider_spin.setDecimals(1)
        self.divider_spin.setToolTip("Share of the frame height given to the top region")
        self.swap_button = QPushButton("Swap top / bottom", self)
        self.reset_button = QPushButton("Reset composition", self)
        self.output_label = QLabel(self)
        self.output_label.setObjectName("muted")

        preset_card = Card("Preset", "Save the crop and layout of a streamer to reuse it on the next clip.", self)
        preset_card.add_row(preset_row)

        region_card = Card("Region", "Pick a region, then drag it in the Original view or type exact values.", self)
        region_card.add_row(region_row)
        region_card.add_row(self.enabled_check)
        region_card.add_row(_pair("X", self.crop_x, "Y", self.crop_y))
        region_card.add_row(_pair("W", self.crop_w, "H", self.crop_h))
        region_card.add_row(self.match_button)

        placement_card = Card("Placement in output", parent=self)
        placement_card.add_row("Mode", self.fit_combo)
        placement_card.add_row("Scale", self.scale_spin)
        placement_card.add_row(_pair("X", self.offset_x, "Y", self.offset_y))
        placement_card.add_row(self.reset_placement_button)

        layout_card = Card("Layout", parent=self)
        layout_card.add_row("Top height", self.divider_spin)
        swap_row = QHBoxLayout()
        swap_row.addWidget(self.swap_button)
        swap_row.addWidget(self.reset_button)
        layout_card.add_row(swap_row)
        layout_card.add_row("Output", self.output_label)

        # ---- parts: after Split every part can have its own layout
        self.part_label = QLabel(self)
        self.part_label.setWordWrap(True)
        self.part_label.setObjectName("muted")
        self.apply_all_button = QPushButton("Use this layout for all parts", self)
        self.apply_all_button.setToolTip("Every part of the clip gets the layout shown now")
        self.apply_all_button.clicked.connect(self.apply_all_requested)
        self.part_card = Card("Part", parent=self)
        self.part_card.add_row(self.part_label)
        self.part_card.add_row(self.apply_all_button)
        self.part_card.setVisible(False)

        # ---- face: auto-framing of the webcam
        self.autoframe_button = QPushButton("Auto-frame face", self)
        self.autoframe_button.setObjectName("primary")
        self.autoframe_button.setToolTip("Find the streamer's face and frame the webcam crop around it "
                                         "(head and shoulders, the slot's shape)")
        self.follow_check = QCheckBox("Follow the face when it moves", self)
        self.follow_check.setChecked(True)
        self.clear_track_button = QPushButton("Stop following", self)
        self.clear_track_button.setToolTip("Keep the crop where it is now and forget the face motion")
        self.face_status = QLabel(self)
        self.face_status.setObjectName("muted")
        self.face_status.setWordWrap(True)
        self.face_card = Card("Face", "Frames the webcam automatically. Runs offline on your PC.", self)
        self.face_card.add_row(self.autoframe_button)
        self.face_card.add_row(self.follow_check)
        self.face_card.add_row(self.clear_track_button)
        self.face_card.add_row(self.face_status)
        self._autoframe_busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        for card in (self.part_card, preset_card, region_card, self.face_card, placement_card, layout_card):
            layout.addWidget(card)
        layout.addStretch(1)

        # ---- wiring
        self._region_group.buttonClicked.connect(
            lambda button: self.controller.select(button.property("region"))
        )
        self.enabled_check.toggled.connect(self._on_enabled)
        for spin in (self.crop_x, self.crop_y, self.crop_w, self.crop_h):
            spin.valueChanged.connect(self._on_crop_edited)
        self.fit_combo.currentIndexChanged.connect(self._on_placement_edited)
        for spin in (self.scale_spin, self.offset_x, self.offset_y):
            spin.valueChanged.connect(self._on_placement_edited)
        self.match_button.clicked.connect(lambda: self.controller.match_aspect(self.controller.selected))
        self.reset_placement_button.clicked.connect(
            lambda: self.controller.reset_placement(self.controller.selected)
        )
        self.autoframe_button.clicked.connect(self._on_autoframe)
        self.clear_track_button.clicked.connect(lambda: self.controller.clear_track(WEBCAM))
        self.divider_spin.valueChanged.connect(self._on_divider_edited)
        self.swap_button.clicked.connect(self.controller.swap)
        self.reset_button.clicked.connect(self.controller.reset)
        self.preset_combo.activated.connect(self._on_preset_chosen)
        self.save_preset_button.clicked.connect(self._on_save_preset)
        self.delete_preset_button.clicked.connect(self._on_delete_preset)

        controller.changed.connect(self.refresh)
        controller.time_changed.connect(self.refresh)
        controller.selection_changed.connect(lambda _name: self.refresh())
        self._reload_presets()
        self.refresh()

    # ------------------------------------------------------------ face
    def _on_autoframe(self) -> None:
        if self._autoframe_busy:
            self.autoframe_cancel_requested.emit()
        else:
            self.autoframe_requested.emit(self.follow_check.isChecked())

    def set_autoframe_busy(self, busy: bool, message: str = "") -> None:
        self._autoframe_busy = busy
        self.autoframe_button.setText("Cancel" if busy else "Auto-frame face")
        self.follow_check.setEnabled(not busy)
        if message or not busy:
            self.face_status.setText(message)
        if not busy:
            self.refresh()
            if message:
                self.face_status.setText(message)

    # ------------------------------------------------------------ helpers
    def _spin(self, minimum: int = 0) -> QSpinBox:
        spin = QSpinBox(self)
        spin.setRange(minimum, 16384)
        spin.setKeyboardTracking(False)
        return spin

    def _offset_spin(self) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setRange(-100, 100)
        spin.setSuffix(" %")
        spin.setDecimals(0)
        spin.setSingleStep(5)
        return spin

    def _reload_presets(self, select: str | None = None) -> None:
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for preset in self.presets.all():
            self.preset_combo.addItem(preset.name)
        index = self.preset_combo.findText(select) if select else -1
        self.preset_combo.setCurrentIndex(index)
        self.preset_combo.blockSignals(False)
        self._update_delete_enabled()

    def _update_delete_enabled(self) -> None:
        name = self.preset_combo.currentText()
        self.delete_preset_button.setEnabled(bool(name) and not self.presets.is_builtin(name))

    # ----------------------------------------------------- model → widgets
    def refresh(self) -> None:
        self._updating = True
        try:
            name = self.controller.selected
            comp = self.controller.composition
            region = comp.region(name)
            sw, sh = self.controller.source_size
            crop = self.controller.crop_px(name)

            self.webcam_button.setChecked(name == WEBCAM)
            self.gameplay_button.setChecked(name == GAMEPLAY)
            self.enabled_check.setChecked(region.enabled)

            self.crop_x.setMaximum(sw)
            self.crop_w.setMaximum(sw)
            self.crop_y.setMaximum(sh)
            self.crop_h.setMaximum(sh)
            self.crop_x.setValue(round(crop.x))
            self.crop_y.setValue(round(crop.y))
            self.crop_w.setValue(round(crop.w))
            self.crop_h.setValue(round(crop.h))

            self.fit_combo.setCurrentIndex(self.fit_combo.findData(region.fit.value))
            self.scale_spin.setValue(region.scale * 100)
            self.offset_x.setValue(region.offset_x * 100)
            self.offset_y.setValue(region.offset_y * 100)

            tracked = bool(comp.webcam.track)
            self.clear_track_button.setVisible(tracked)
            if not self._autoframe_busy:
                self.face_status.setText(
                    f"Following the face ({len(comp.webcam.track)} keys). Drag the crop to adjust; "
                    "the motion is kept." if tracked else "")
            self.face_card.setVisible(comp.webcam.enabled)

            self.divider_spin.setValue(comp.divider * 100)
            both = len(comp.slots()) == 2
            self.divider_spin.setEnabled(both)
            self.swap_button.setEnabled(both)
            for widget in (
                self.crop_x, self.crop_y, self.crop_w, self.crop_h, self.fit_combo,
                self.scale_spin, self.offset_x, self.offset_y, self.match_button,
                self.reset_placement_button,
            ):
                widget.setEnabled(region.enabled)
            self.output_label.setText(f"{comp.output_w} × {comp.output_h}  ·  source {sw} × {sh}")
        finally:
            self._updating = False

    # ----------------------------------------------------- widgets → model
    def _on_enabled(self, checked: bool) -> None:
        if not self._updating:
            self.controller.update_region(self.controller.selected, enabled=checked)

    def _on_crop_edited(self) -> None:
        if self._updating:
            return
        rect = Rect(self.crop_x.value(), self.crop_y.value(), self.crop_w.value(), self.crop_h.value())
        self.controller.set_crop_px(self.controller.selected, rect)

    def _on_placement_edited(self) -> None:
        if self._updating:
            return
        self.controller.update_region(
            self.controller.selected,
            fit=FitMode(self.fit_combo.currentData()),
            scale=self.scale_spin.value() / 100,
            offset_x=self.offset_x.value() / 100,
            offset_y=self.offset_y.value() / 100,
        )

    def _on_divider_edited(self, value: float) -> None:
        if not self._updating:
            self.controller.set_divider(value / 100)

    # ------------------------------------------------------------ presets
    def _on_preset_chosen(self, _index: int) -> None:
        preset = self.presets.get(self.preset_combo.currentText())
        if preset is not None:
            self.controller.set_composition(preset.composition)
        self._update_delete_enabled()

    def _on_save_preset(self) -> None:
        current = self.preset_combo.currentText()
        suggestion = "" if self.presets.is_builtin(current) else current
        name, ok = QInputDialog.getText(self, "Save preset", "Preset name:", text=suggestion)
        if ok:
            self.save_preset(name)

    def save_preset(self, name: str) -> bool:
        try:
            self.presets.save(name, self.controller.composition)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Preset not saved", str(exc))
            return False
        self._reload_presets(select=name.strip())
        return True

    def _on_delete_preset(self) -> None:
        name = self.preset_combo.currentText()
        if name and self.presets.delete(name):
            self._reload_presets()

    def set_part_info(self, index: int, count: int, own: bool, any_own: bool) -> None:
        """Which part of the clip the editor shows (index from 0)."""
        self.part_card.setVisible(count > 1 or any_own)
        if count > 1:
            text = (f"Editing the layout of part {index + 1} of {count} (under the playhead). "
                    + ("It has its own layout." if own else "It uses the common layout until you change it."))
        else:
            text = "This part has its own layout."
        self.part_label.setText(text)
        self.apply_all_button.setEnabled(any_own)

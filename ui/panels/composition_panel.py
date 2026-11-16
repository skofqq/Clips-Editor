"""Properties for the Stacked Layout: presets, regions, divider."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
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


def _title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionTitle")
    return label


class CompositionPanel(QWidget):
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

        form = QFormLayout()
        form.setVerticalSpacing(6)
        form.addRow(_title("Preset"))
        form.addRow(preset_row)
        form.addRow(_title("Region"))
        form.addRow(region_row)
        form.addRow(self.enabled_check)
        form.addRow(_title("Crop in source (px)"))
        form.addRow("X", self.crop_x)
        form.addRow("Y", self.crop_y)
        form.addRow("Width", self.crop_w)
        form.addRow("Height", self.crop_h)
        form.addRow(self.match_button)
        form.addRow(_title("Placement in output"))
        form.addRow("Mode", self.fit_combo)
        form.addRow("Scale", self.scale_spin)
        form.addRow("Offset X", self.offset_x)
        form.addRow("Offset Y", self.offset_y)
        form.addRow(self.reset_placement_button)
        form.addRow(_title("Layout"))
        form.addRow("Top height", self.divider_spin)
        form.addRow(self.swap_button)
        form.addRow(self.reset_button)
        form.addRow("Output", self.output_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(form)
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
        self.divider_spin.valueChanged.connect(self._on_divider_edited)
        self.swap_button.clicked.connect(self.controller.swap)
        self.reset_button.clicked.connect(self.controller.reset)
        self.preset_combo.activated.connect(self._on_preset_chosen)
        self.save_preset_button.clicked.connect(self._on_save_preset)
        self.delete_preset_button.clicked.connect(self._on_delete_preset)

        controller.changed.connect(self.refresh)
        controller.selection_changed.connect(lambda _name: self.refresh())
        self._reload_presets()
        self.refresh()

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

"""Blur panel: add blur / pixelate boxes, tune them and make them follow an object.

The boxes live in the timeline (their own Blur track); the box itself is
moved and resized on the Original view, its time on the Blur track.
"""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.editor.blur import BLUR, PIXELATE
from core.timeline.timebase import format_timecode
from ui.timeline.timeline_controller import BLUR as BLUR_KIND
from ui.timeline.timeline_controller import TimelineController
from ui.widgets.card import Card
from ui.widgets.jump_slider import JumpSlider

ROLE_ID = Qt.ItemDataRole.UserRole + 1
MODE_TEXT = {BLUR: "Blur", PIXELATE: "Pixelate"}


class BlurPanel(QScrollArea):
    follow_requested = Signal(int)  # blur id
    follow_cancel_requested = Signal()
    seek_requested = Signal(int)  # source microseconds
    edit_finished = Signal()  # closes the undo step of a slider drag

    def __init__(self, timeline: TimelineController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.timeline = timeline
        self._busy = False
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page = QWidget(self)
        column = QVBoxLayout(page)
        column.setContentsMargins(8, 8, 8, 8)
        column.setSpacing(10)

        # ------------------------------------------------------------ boxes
        self.boxes_card = Card("Blur boxes", "Hide a face, chat, a name or anything else. A box stays on its "
                               "object through cuts and layouts.", page)
        self.add_button = QPushButton("Add blur", self.boxes_card)
        self.add_button.setObjectName("primary")
        self.add_button.setToolTip("New box at the playhead (or over the selected range). Shortcut: B")
        self.delete_button = QPushButton("Delete", self.boxes_card)
        row = QHBoxLayout()
        row.addWidget(self.add_button, 1)
        row.addWidget(self.delete_button)
        self.boxes_card.body.addLayout(row)
        self.list = QListWidget(self.boxes_card)
        self.list.setFixedHeight(48)
        self.boxes_card.body.addWidget(self.list)
        self.hint = QLabel("", self.boxes_card)
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        self.boxes_card.body.addWidget(self.hint)
        column.addWidget(self.boxes_card)

        # ----------------------------------------------------------- effect
        self.effect_card = Card("Effect", parent=page, label_width=70)
        self.mode_combo = QComboBox(self.effect_card)
        for mode in (BLUR, PIXELATE):
            self.mode_combo.addItem(MODE_TEXT[mode], mode)
        self.effect_card.add_row("Look", self.mode_combo)
        self.strength_slider, self.strength_label = self._slider(1, 100)
        self.effect_card.add_row("Strength", self._with_label(self.strength_slider, self.strength_label))
        self.feather_slider, self.feather_label = self._slider(0, 100)
        self.feather_slider.setToolTip("How softly the box fades into the picture at its edges.")
        self.effect_card.add_row("Soft edges", self._with_label(self.feather_slider, self.feather_label))
        column.addWidget(self.effect_card)

        # ------------------------------------------------------------- time
        self.time_card = Card("Time", "Drag the box on the Blur track to move it, its edges to change "
                              "how long it lasts.", page)
        self.time_label = QLabel("", self.time_card)
        self.time_card.body.addWidget(self.time_label)
        self.start_button = QPushButton("Start at playhead", self.time_card)
        self.end_button = QPushButton("End at playhead", self.time_card)
        row = QHBoxLayout()
        row.addWidget(self.start_button)
        row.addWidget(self.end_button)
        self.time_card.body.addLayout(row)
        column.addWidget(self.time_card)

        # ----------------------------------------------------------- follow
        self.follow_card = Card("Follow an object", "Put the box over the object on the Original view, then "
                                "press Follow: it is found in every frame of the blur and the box moves with it.",
                                page)
        self.follow_button = QPushButton("Follow object", self.follow_card)
        self.follow_button.setObjectName("primary")
        self.cancel_button = QPushButton("Cancel", self.follow_card)
        self.unfollow_button = QPushButton("Stand still", self.follow_card)
        self.unfollow_button.setToolTip("Forget the motion: the box stays where it is now.")
        row = QHBoxLayout()
        row.addWidget(self.follow_button, 1)
        row.addWidget(self.cancel_button)
        row.addWidget(self.unfollow_button)
        self.follow_card.body.addLayout(row)
        self.follow_status = QLabel("", self.follow_card)
        self.follow_status.setObjectName("muted")
        self.follow_status.setWordWrap(True)
        self.follow_card.body.addWidget(self.follow_status)
        column.addWidget(self.follow_card)
        column.addStretch(1)
        self.setWidget(page)

        self.add_button.clicked.connect(lambda: self.timeline.add_blur())
        self.delete_button.clicked.connect(self._delete)
        self.list.currentItemChanged.connect(self._on_list_selection)
        self.mode_combo.currentIndexChanged.connect(lambda _i: self._change(mode=self.mode_combo.currentData()))
        self.strength_slider.valueChanged.connect(lambda v: self._change("strength", strength=v))
        self.feather_slider.valueChanged.connect(lambda v: self._change("feather", feather=v))
        for slider in (self.strength_slider, self.feather_slider):
            slider.sliderReleased.connect(self.edit_finished)
        self.start_button.clicked.connect(lambda: self._change(start_us=self.timeline.playhead_us))
        self.end_button.clicked.connect(lambda: self._change(end_us=self.timeline.playhead_us))
        self.follow_button.clicked.connect(self._follow)
        self.cancel_button.clicked.connect(self.follow_cancel_requested)
        self.unfollow_button.clicked.connect(self._stand_still)
        timeline.changed.connect(self.refresh)
        timeline.selection_changed.connect(self.refresh)
        timeline.playhead_changed.connect(lambda _us: self._refresh_hint())
        self.refresh()

    # ---------------------------------------------------------- helpers
    def _slider(self, low: int, high: int) -> tuple[JumpSlider, QLabel]:
        slider = JumpSlider(Qt.Orientation.Horizontal, self)
        slider.setRange(low, high)
        label = QLabel("", self)
        label.setMinimumWidth(30)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        slider.valueChanged.connect(lambda v: label.setText(str(v)))
        return slider, label

    def _with_label(self, slider: JumpSlider, label: QLabel) -> QWidget:
        box = QWidget(self)
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(slider, 1)
        row.addWidget(label)
        return box

    def selected(self):
        return self.timeline.selected_blur()

    def _change(self, key: str | None = None, **changes) -> None:
        region = self.selected()
        if region is not None:
            self.timeline.update_blur(region.id, key=(key, region.id) if key else None, **changes)

    def _delete(self) -> None:
        if self.selected() is not None:
            self.timeline.delete_selection()

    def _follow(self) -> None:
        region = self.selected()
        if region is not None:
            self.follow_requested.emit(region.id)

    def _stand_still(self) -> None:
        region = self.selected()
        if region is None or not region.track:
            return
        now = region.rect_at(self.timeline.playhead_us)
        self.timeline.update_blur(region.id, rect=now, track=())
        self.follow_status.setText("The box stands still now.")

    def _on_list_selection(self, item: QListWidgetItem | None, _previous=None) -> None:
        if item is None:
            return
        blur_id = item.data(ROLE_ID)
        t = self.timeline.timeline
        region = t.blur(blur_id) if t is not None else None
        if region is None:
            return
        self.timeline.select(BLUR_KIND, blur_id)
        if not region.active(self.timeline.playhead_us):
            self.seek_requested.emit(region.start_us)

    # ----------------------------------------------------------- state
    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        if message or not busy:
            self.follow_status.setText(message)
        self.refresh()

    def refresh(self) -> None:
        t = self.timeline.timeline
        region = self.selected()
        blurs = t.blurs if t is not None else ()
        with QSignalBlocker(self.list):
            self.list.clear()
            for number, b in enumerate(blurs, 1):
                text = (f"{number}. {MODE_TEXT[b.mode]}  {format_timecode(b.start_us)} – {format_timecode(b.end_us)}"
                        + ("  · follows" if b.track else ""))
                item = QListWidgetItem(text)
                item.setData(ROLE_ID, b.id)
                self.list.addItem(item)
                if region is not None and b.id == region.id:
                    self.list.setCurrentItem(item)
        self.list.setFixedHeight(min(190, 14 + 36 * max(1, len(blurs))))
        self.list.setVisible(bool(blurs))
        has = region is not None
        self.add_button.setEnabled(t is not None and not self._busy)
        self.delete_button.setEnabled(has and not self._busy)
        for card in (self.effect_card, self.time_card):
            card.setEnabled(has and not self._busy)
        self.follow_button.setEnabled(has and not self._busy)
        self.cancel_button.setVisible(self._busy)
        self.unfollow_button.setEnabled(has and bool(region.track) and not self._busy)
        with QSignalBlocker(self.mode_combo), QSignalBlocker(self.strength_slider), QSignalBlocker(self.feather_slider):
            if has:
                self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(region.mode)))
                self.strength_slider.setValue(region.strength)
                self.feather_slider.setValue(region.feather)
            self.strength_label.setText(str(self.strength_slider.value()))
            self.feather_label.setText(str(self.feather_slider.value()))
        if has:
            length = (region.end_us - region.start_us) / 1_000_000
            self.time_label.setText(f"From {format_timecode(region.start_us)} to {format_timecode(region.end_us)}"
                                    f"  ({length:.1f} s)")
        else:
            self.time_label.setText("")
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        t = self.timeline.timeline
        region = self.selected()
        if t is None:
            text = "Import a video first."
        elif not t.blurs:
            text = "No blur yet. Press Add blur, then drag the teal box over what to hide on the Original view."
        elif region is None:
            text = "Pick a box in the list or on the Blur track to change it."
        elif not region.active(self.timeline.playhead_us):
            text = "The playhead is outside this box's time: pick it in the list to jump there."
        else:
            text = "Drag the teal box on the Original view to move it, its handles to resize it."
        self.hint.setText(text)

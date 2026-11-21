"""Censor panel: find swear words, review every suggestion, then apply.

Nothing here mutes audio by itself. Suggestions start as *pending*; only
the ones the user accepts are turned into Mute Regions, and only when the
user presses **Apply to timeline**.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.censor.dictionary import CUSTOM, MILD, STRONG
from core.censor.review import ACCEPTED, CHECK, PENDING, REJECTED, CensorSettings
from core.timeline.model import BEEP, SILENCE
from core.timeline.timebase import format_timecode
from ui.theme import palette
from ui.censor.censor_controller import CensorController
from ui.widgets.card import Card
from ui.widgets.empty_state import EmptyState

COL_TIME, COL_WORD, COL_MATCH, COL_STATUS = range(4)
ROLE_ID = Qt.ItemDataRole.UserRole + 1

STATUS_TEXT = {PENDING: "To review", ACCEPTED: "Accepted", REJECTED: "Rejected"}
STATUS_COLOR = {PENDING: QColor(palette.CAUTION), ACCEPTED: QColor(palette.CRITICAL), REJECTED: QColor(palette.TEXT_TERTIARY)}
CATEGORY_TEXT = {STRONG: "strong", MILD: "mild", CUSTOM: "custom"}


class CensorPanel(QWidget):
    detect_requested = Signal()
    apply_requested = Signal()
    remove_requested = Signal()
    exclude_requested = Signal(str)  # word to add to the exclusions
    word_edited = Signal(int, int, str)  # segment id, word index, new text
    preview_requested = Signal(int, int)  # start_us, end_us
    dictionary_requested = Signal()
    range_edit_finished = Signal()  # closes the undo step of a Start/End edit

    def __init__(self, controller: CensorController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._rebuilding = False
        self._has_transcript = False
        self._applied = True

        # ----------------------------------------------------- settings
        self.strong_check = QCheckBox("Strong (mat, slurs)", self)
        self.strong_check.setChecked(True)
        self.mild_check = QCheckBox("Mild (жопа, damn…)", self)
        self.custom_check = QCheckBox("My words", self)
        self.custom_check.setChecked(True)
        self.before_spin = self._ms_spin(
            "Silence starts this long before the word.\nNegative: starts inside the word (keeps its first sound).\nCapped at 1/4 of the word; words under 0.2 s are muted whole."
        )
        self.after_spin = self._ms_spin(
            "Silence ends this long after the word.\nNegative: ends inside the word (keeps its last sound).\nCapped at 1/4 of the word; words under 0.2 s are muted whole."
        )
        self.mode_combo = QComboBox(self)
        self.mode_combo.addItem("Silence", SILENCE)
        self.mode_combo.addItem("Beep", BEEP)
        self.detect_button = QPushButton("Find swear words", self)
        self.detect_button.setObjectName("primary")
        self.detect_button.setToolTip("Scan the transcript. Results are suggestions only: nothing is muted yet.")
        self.dictionary_button = QPushButton("Dictionary…", self)
        self.dictionary_button.setToolTip("Your own words to censor and words never to censor")

        # ----------------------------------------------------- table
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["Time", "Word", "Match", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_TIME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_WORD, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_MATCH, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)

        # ----------------------------------------------------- selected
        self.start_spin = self._time_spin()
        self.end_spin = self._time_spin()
        self.accept_button = QPushButton("Accept (A)", self)
        self.reject_button = QPushButton("Reject (R)", self)
        self.preview_button = QPushButton("Listen (P)", self)
        self.preview_button.setToolTip("Play from just before to just after this suggestion")
        self.exclude_button = QPushButton("Never censor this word", self)
        self.exclude_button.setToolTip("Add the word to your exclusions and drop its suggestions")
        self.accept_all_button = QPushButton("Accept all to review", self)
        self.reject_all_button = QPushButton("Reject all to review", self)
        self.apply_button = QPushButton("Apply to timeline", self)
        self.apply_button.setObjectName("primary")
        self.apply_button.setToolTip("Create Mute Regions for accepted suggestions only")
        self.remove_button = QPushButton("Remove censor mutes", self)
        self.remove_button.setToolTip("Delete the Mute Regions created by the censor (your own mutes stay)")
        self.summary_label = QLabel(self)
        self.summary_label.setObjectName("muted")
        self.summary_label.setWordWrap(True)
        self.hint_label = QLabel(self)
        self.hint_label.setObjectName("muted")
        self.hint_label.setWordWrap(True)

        # ----------------------------------------------------- layout
        cats = QHBoxLayout()
        for check in (self.strong_check, self.mild_check, self.custom_check):
            cats.addWidget(check)
        cats.addStretch(1)
        settings = QFormLayout()
        settings.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        pads = QHBoxLayout()
        pads.addWidget(QLabel("before", self))
        pads.addWidget(self.before_spin, 1)
        pads.addWidget(QLabel("after", self))
        pads.addWidget(self.after_spin, 1)
        settings.addRow("Padding", pads)
        settings.addRow("Mode", self.mode_combo)
        top = QHBoxLayout()
        top.addWidget(self.detect_button, 1)
        top.addWidget(self.dictionary_button)

        edit = QGridLayout()
        edit.addWidget(QLabel("Start", self), 0, 0)
        edit.addWidget(self.start_spin, 0, 1)
        edit.addWidget(QLabel("End", self), 0, 2)
        edit.addWidget(self.end_spin, 0, 3)
        edit.addWidget(self.accept_button, 1, 0, 1, 2)
        edit.addWidget(self.reject_button, 1, 2, 1, 2)
        edit.addWidget(self.preview_button, 2, 0, 1, 2)
        edit.addWidget(self.exclude_button, 2, 2, 1, 2)
        bulk = QHBoxLayout()
        bulk.addWidget(self.accept_all_button)
        bulk.addWidget(self.reject_all_button)
        apply = QHBoxLayout()
        apply.addWidget(self.apply_button, 1)
        apply.addWidget(self.remove_button)

        find_card = Card("Find swear words", parent=self)
        find_card.body.addLayout(cats)
        find_card.body.addLayout(settings)
        find_card.body.addLayout(top)

        self.review_card = Card("Review the selected word", parent=self)
        self.review_card.body.addWidget(self.hint_label)
        self.review_card.body.addLayout(edit)
        self.review_card.body.addLayout(bulk)

        self.empty = EmptyState("shield", "Nothing to review yet", parent=self)
        self.list_stack = QStackedWidget(self)
        self.list_stack.addWidget(self.empty)
        self.list_stack.addWidget(self.table)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addWidget(find_card)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.list_stack, 1)
        layout.addWidget(self.review_card)
        layout.addLayout(apply)

        self.set_settings(CensorSettings())

        # ----------------------------------------------------- wiring
        self.detect_button.clicked.connect(self.detect_requested)
        self.dictionary_button.clicked.connect(self.dictionary_requested)
        self.apply_button.clicked.connect(self.apply_requested)
        self.remove_button.clicked.connect(self.remove_requested)
        self.accept_button.clicked.connect(lambda: self.review_selected(ACCEPTED))
        self.reject_button.clicked.connect(lambda: self.review_selected(REJECTED))
        self.preview_button.clicked.connect(self.preview_current)
        self.exclude_button.clicked.connect(self._exclude_current)
        self.accept_all_button.clicked.connect(lambda: controller.set_all_status(ACCEPTED, only=PENDING))
        self.reject_all_button.clicked.connect(lambda: controller.set_all_status(REJECTED, only=PENDING))
        self.start_spin.valueChanged.connect(lambda v: self._range_edited(start=v))
        self.end_spin.valueChanged.connect(lambda v: self._range_edited(end=v))
        self.start_spin.editingFinished.connect(self._close_range_step)
        self.end_spin.editingFinished.connect(self._close_range_step)
        for widget in (self.strong_check, self.mild_check, self.custom_check):
            widget.toggled.connect(self._settings_edited)
        self.before_spin.valueChanged.connect(self._settings_edited)
        self.after_spin.valueChanged.connect(self._settings_edited)
        self.mode_combo.currentIndexChanged.connect(self._settings_edited)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        controller.changed.connect(self.rebuild)

        for key, slot in (
            ("A", lambda: self.review_selected(ACCEPTED)),
            ("R", lambda: self.review_selected(REJECTED)),
            ("P", self.preview_current),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
        self.rebuild()

    # ------------------------------------------------------------ widgets
    def _ms_spin(self, tip: str) -> QSpinBox:
        spin = QSpinBox(self)
        spin.setRange(-1000, 2000)
        spin.setSingleStep(10)
        spin.setSuffix(" ms")
        spin.setToolTip(tip)
        return spin

    def _time_spin(self) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setDecimals(3)
        spin.setSingleStep(0.01)
        spin.setRange(0, 0)
        spin.setSuffix(" s")
        spin.setKeyboardTracking(False)
        return spin

    # ------------------------------------------------------------ settings
    def settings(self) -> CensorSettings:
        categories = tuple(
            cat for cat, check in ((STRONG, self.strong_check), (MILD, self.mild_check), (CUSTOM, self.custom_check))
            if check.isChecked()
        )
        return CensorSettings(
            pad_before_us=self.before_spin.value() * 1000,
            pad_after_us=self.after_spin.value() * 1000,
            mode=self.mode_combo.currentData(),
            categories=categories,
        )

    def set_settings(self, settings: CensorSettings) -> None:
        self._rebuilding = True
        try:
            self._apply_settings(settings)
        finally:
            self._rebuilding = False

    def _apply_settings(self, settings: CensorSettings) -> None:
        self.strong_check.setChecked(STRONG in settings.categories)
        self.mild_check.setChecked(MILD in settings.categories)
        self.custom_check.setChecked(CUSTOM in settings.categories)
        self.before_spin.setValue(settings.pad_before_us // 1000)
        self.after_spin.setValue(settings.pad_after_us // 1000)
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(settings.mode)))

    def _settings_edited(self) -> None:
        if self._rebuilding:
            return
        state = self.controller.state
        if state is None:
            return
        settings = self.settings()
        if settings.categories != state.settings.categories:
            # Different categories need a new scan; keep the rest in sync now.
            settings = CensorSettings(settings.pad_before_us, settings.pad_after_us, settings.mode,
                                      state.settings.categories)
            self.hint_label.setText("Categories changed: press “Find swear words” to scan again.")
        updated = state.with_settings(settings, self.controller.duration_us)
        self.controller.set_state(updated, key="censor_settings")

    # ------------------------------------------------------------ state
    def set_has_transcript(self, has: bool) -> None:
        self._has_transcript = has
        self._update_summary()
        self._update_buttons()

    def set_applied(self, applied: bool) -> None:
        self._applied = applied
        self._update_summary()
        self._update_buttons()

    def selected_ids(self) -> list[int]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [self.table.item(r, COL_TIME).data(ROLE_ID) for r in rows]

    def rebuild(self) -> None:
        state = self.controller.state
        keep = self.selected_ids() if self.table.rowCount() else []
        self._rebuilding = True
        try:
            if state is not None:
                self._apply_settings(state.settings)
            items = state.suggestions if state is not None else ()
            self.table.setRowCount(len(items))
            for row, sug in enumerate(items):
                time_text = format_timecode(sug.word_start_us)
                if not sug.exact:
                    time_text = "≈ " + time_text
                time_item = QTableWidgetItem(time_text)
                time_item.setData(ROLE_ID, sug.id)
                time_item.setFlags(time_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if not sug.exact:
                    time_item.setToolTip("Approximate timing: check the start and end before accepting.")
                word_item = QTableWidgetItem(sug.text)
                word_item.setToolTip("Double-click to correct the recognised word")
                match_text = f"{sug.entry_key.split(':', 1)[-1]} · {CATEGORY_TEXT.get(sug.category, sug.category)}"
                if sug.source == CHECK:
                    match_text += " · 2nd model"
                    word_item.setFlags(word_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    word_item.setToolTip(
                        "Only the double-check model heard this word; the transcript has something else here.\n"
                        "Listen (P) before accepting.")
                match_item = QTableWidgetItem(match_text)
                if sug.source == CHECK:
                    match_item.setToolTip(word_item.toolTip())
                match_item.setFlags(match_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                status_item = QTableWidgetItem(STATUS_TEXT[sug.status])
                status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                status_item.setForeground(QBrush(STATUS_COLOR[sug.status]))
                for col, item in enumerate((time_item, word_item, match_item, status_item)):
                    self.table.setItem(row, col, item)
            self.table.clearSelection()
            model, selection = self.table.model(), self.table.selectionModel()
            flags = selection.SelectionFlag.Select | selection.SelectionFlag.Rows
            for row, sug in enumerate(items):
                if sug.id in keep:
                    selection.select(model.index(row, 0), flags)
        finally:
            self._rebuilding = False
        self._show_current()
        self._update_summary()
        self._update_buttons()

    def _update_empty_state(self) -> None:
        state = self.controller.state
        has_items = state is not None and bool(state.suggestions)
        self.list_stack.setCurrentWidget(self.table if has_items else self.empty)
        self.review_card.setVisible(has_items)
        if has_items:
            return
        if not self._has_transcript:
            self.empty.set_text("Transcribe the clip first",
                                "The censor looks for swear words in the transcript (Transcript tab).")
        elif state is None:
            self.empty.set_text("Nothing to review yet",
                                "Press “Find swear words”. You review every result before anything is muted.")
        else:
            self.empty.set_text("Clean clip", "No swear words found with the chosen categories.")

    def _update_summary(self) -> None:
        self._update_empty_state()
        state = self.controller.state
        if state is None:
            self.summary_label.setText("")
            self.summary_label.setVisible(False)
            return
        self.summary_label.setVisible(True)
        if not state.suggestions:
            self.summary_label.setVisible(False)
            return
        text = (
            f"{len(state.suggestions)} found · {state.count(PENDING)} to review · "
            f"{state.count(ACCEPTED)} accepted · {state.count(REJECTED)} rejected"
        )
        if not self._applied:
            text += "\nChanges are not on the timeline yet: press “Apply to timeline”."
        self.summary_label.setText(text)

    def _update_buttons(self) -> None:
        state = self.controller.state
        has_items = state is not None and bool(state.suggestions)
        selected = bool(self.selected_ids()) if self.table.rowCount() else False
        single = self.controller.current is not None
        self.detect_button.setEnabled(self._has_transcript)
        for button in (self.accept_button, self.reject_button):
            button.setEnabled(selected)
        for widget in (self.preview_button, self.exclude_button, self.start_spin, self.end_spin):
            widget.setEnabled(single)
        self.accept_all_button.setEnabled(has_items and state.count(PENDING) > 0)
        self.reject_all_button.setEnabled(has_items and state.count(PENDING) > 0)
        self.apply_button.setEnabled(state is not None and not self._applied)
        self.remove_button.setEnabled(state is not None and bool(state.applied_mute_ids))

    # ------------------------------------------------------------ selection
    def _on_selection(self) -> None:
        if self._rebuilding:
            return
        ids = self.selected_ids()
        self.controller.set_current(ids[0] if len(ids) == 1 else None)
        self._show_current()
        self._update_buttons()

    def select_suggestion(self, suggestion_id: int) -> None:
        for row in range(self.table.rowCount()):
            if self.table.item(row, COL_TIME).data(ROLE_ID) == suggestion_id:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, COL_TIME))
                return

    def _show_current(self) -> None:
        state, current = self.controller.state, self.controller.current
        sug = state.get(current) if state is not None and current is not None else None
        self._rebuilding = True
        try:
            limit = self.controller.duration_us / 1e6
            for spin in (self.start_spin, self.end_spin):
                spin.setRange(0, max(0.0, limit))
            if sug is None:
                self.start_spin.setValue(0)
                self.end_spin.setValue(0)
                self.hint_label.clear()
            else:
                self.start_spin.setValue(sug.start_us / 1e6)
                self.end_spin.setValue(sug.end_us / 1e6)
                if not sug.exact:
                    self.hint_label.setText("≈ This word has approximate timing. Listen and adjust Start/End.")
                else:
                    self.hint_label.setText(
                        f"Word {format_timecode(sug.word_start_us)} – {format_timecode(sug.word_end_us)}"
                    )
        finally:
            self._rebuilding = False

    # ------------------------------------------------------------ actions
    def review_selected(self, status: str) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        self.controller.set_status(ids, status)
        # Move on to the next suggestion so A/R can be pressed in a row.
        if len(ids) == 1 and rows and rows[-1] + 1 < self.table.rowCount():
            self.table.selectRow(rows[-1] + 1)

    def preview_current(self) -> None:
        state, current = self.controller.state, self.controller.current
        sug = state.get(current) if state is not None and current is not None else None
        if sug is not None:
            self.preview_requested.emit(sug.start_us, sug.end_us)

    def _exclude_current(self) -> None:
        state, current = self.controller.state, self.controller.current
        sug = state.get(current) if state is not None and current is not None else None
        if sug is not None:
            self.exclude_requested.emit(sug.text)

    def _range_edited(self, start: float | None = None, end: float | None = None) -> None:
        current = self.controller.current
        if self._rebuilding or current is None:
            return
        self.controller.set_range(
            current,
            None if start is None else round(start * 1e6),
            None if end is None else round(end * 1e6),
        )

    def _close_range_step(self) -> None:
        self.range_edit_finished.emit()

    def _on_double_click(self, row: int, col: int) -> None:
        if col != COL_WORD:
            item = self.table.item(row, COL_TIME)
            state = self.controller.state
            sug = state.get(item.data(ROLE_ID)) if state is not None and item is not None else None
            if sug is not None:
                self.preview_requested.emit(sug.start_us, sug.end_us)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._rebuilding or item.column() != COL_WORD:
            return
        state = self.controller.state
        sug = state.get(self.table.item(item.row(), COL_TIME).data(ROLE_ID)) if state else None
        text = " ".join(item.text().split())
        if sug is None or not text or text == sug.text:
            self.rebuild()
            return
        self.word_edited.emit(sug.segment_id, sug.word_index, text)


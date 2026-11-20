"""Highlights panel: find the loud and funny moments, then jump to them,
select them on the timeline or keep only one of them."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.highlights.finder import HIGH, LOW, NORMAL, Highlight
from core.timeline.timebase import format_timecode
from ui.widgets.card import Card

ROLE_INDEX = Qt.ItemDataRole.UserRole + 1


def stars(score: float) -> str:
    filled = 1 + int(score >= 0.6) + int(score >= 0.85)
    return "★" * filled + "☆" * (3 - filled)


class HighlightsPanel(QWidget):
    find_requested = Signal(str)  # sensitivity
    play_requested = Signal(int)  # index into the highlights
    select_requested = Signal(int)
    keep_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._highlights: list[Highlight] = []
        self.sensitivity_combo = QComboBox(self)
        self.sensitivity_combo.addItem("Fewer, strongest moments", LOW)
        self.sensitivity_combo.addItem("Normal", NORMAL)
        self.sensitivity_combo.addItem("More moments", HIGH)
        self.sensitivity_combo.setCurrentIndex(1)
        self.find_button = QPushButton("Find highlights", self)
        self.find_button.setObjectName("primary")
        self.find_button.setToolTip("Looks for loud moments (shouts, screams) and, when the clip is "
                                    "transcribed, laughter, stretched words and hype words")
        self.status = QLabel(self)
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)

        search = Card("Find", "Marks the moments worth a Short on the timeline. Nothing is cut.", self)
        search.add_row("Show", self.sensitivity_combo)
        search.add_row(self.find_button)
        search.add_row(self.status)

        self.list = QListWidget(self)
        self.list.setAlternatingRowColors(False)
        self.list.setMinimumHeight(160)
        self.list.itemDoubleClicked.connect(lambda _item: self._emit(self.play_requested))
        self.play_button = QPushButton("Play", self)
        self.select_button = QPushButton("Select", self)
        self.select_button.setToolTip("Select the moment on the timeline (then T keeps it, M mutes it…)")
        self.keep_button = QPushButton("Keep only this", self)
        self.keep_button.setToolTip("Cut everything else away (undo with Ctrl+Z)")
        buttons = QHBoxLayout()
        for button in (self.play_button, self.select_button, self.keep_button):
            buttons.addWidget(button)
        found = Card("Moments", parent=self)
        found.add_row(self.list)
        found.add_row(buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(search)
        layout.addWidget(found, 1)

        self.find_button.clicked.connect(lambda: self.find_requested.emit(self.sensitivity_combo.currentData()))
        self.play_button.clicked.connect(lambda: self._emit(self.play_requested))
        self.select_button.clicked.connect(lambda: self._emit(self.select_requested))
        self.keep_button.clicked.connect(lambda: self._emit(self.keep_requested))
        self.list.currentRowChanged.connect(lambda _row: self._update_buttons())
        self.set_highlights([])

    @property
    def sensitivity(self) -> str:
        return self.sensitivity_combo.currentData()

    def current_index(self) -> int:
        item = self.list.currentItem()
        return int(item.data(ROLE_INDEX)) if item is not None else -1

    def select_index(self, index: int) -> None:
        for row in range(self.list.count()):
            if self.list.item(row).data(ROLE_INDEX) == index:
                self.list.setCurrentRow(row)
                return

    def _emit(self, signal) -> None:
        index = self.current_index()
        if index >= 0:
            signal.emit(index)

    def _update_buttons(self) -> None:
        has = self.current_index() >= 0
        for button in (self.play_button, self.select_button, self.keep_button):
            button.setEnabled(has)

    def set_busy(self, busy: bool, message: str = "") -> None:
        self.find_button.setEnabled(not busy)
        self.find_button.setText("Looking…" if busy else "Find highlights")
        if message:
            self.status.setText(message)

    def set_enabled_for(self, has_media: bool, has_transcript: bool) -> None:
        self.find_button.setEnabled(has_media)
        if not has_media:
            self.status.setText("Import a video first.")
        elif not has_transcript and not self._highlights:
            self.status.setText("Tip: transcribe the clip first to also find laughter and shouting in the words.")

    def set_highlights(self, highlights) -> None:
        self._highlights = list(highlights or [])
        self.list.clear()
        for index, h in enumerate(self._highlights):
            text = (f"{format_timecode(h.start_us)} – {format_timecode(h.end_us)}   {stars(h.score)}\n"
                    f"{h.label}")
            item = QListWidgetItem(text)
            item.setData(ROLE_INDEX, index)
            item.setToolTip(f"Strongest at {format_timecode(h.peak_us)}")
            self.list.addItem(item)
        if self._highlights:
            self.list.setCurrentRow(0)
        self._update_buttons()

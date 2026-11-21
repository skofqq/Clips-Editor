"""Edit translation: every phrase next to its original; the right column is editable."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.timeline.timebase import format_timecode
from core.transcription.model import Transcript

ROLE_ID = Qt.ItemDataRole.UserRole + 1


class TranslationDialog(QDialog):
    def __init__(self, original: Transcript, translation: Transcript, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit translation")
        self.resize(900, 560)
        self._translation = translation
        originals = {s.id: s for s in original.segments}
        hint = QLabel("Double-click a phrase on the right to correct it. Word timing is spread over the phrase.", self)
        hint.setObjectName("muted")
        self.table = QTableWidget(len(translation.segments), 3, self)
        self.table.setHorizontalHeaderLabels(["Time", "Original", "Translation"])
        self.table.verticalHeader().hide()
        self.table.setWordWrap(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for row, segment in enumerate(translation.segments):
            source = originals.get(segment.id)
            time = QTableWidgetItem(format_timecode(segment.start_us))
            text = QTableWidgetItem(source.text if source is not None else "")
            for item in (time, text):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            mine = QTableWidgetItem(segment.text)
            mine.setData(ROLE_ID, segment.id)
            self.table.setItem(row, 0, time)
            self.table.setItem(row, 1, text)
            self.table.setItem(row, 2, mine)
        self.table.resizeRowsToContents()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
                                   self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.table, 1)
        layout.addWidget(buttons)

    def changes(self) -> dict[int, str]:
        """Segment id → new text, for the phrases the user changed."""
        before = {s.id: s.text for s in self._translation.segments}
        result = {}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 2)
            seg_id = item.data(ROLE_ID)
            text = " ".join(item.text().split())
            if text != before.get(seg_id):
                result[seg_id] = text
        return result

    def edited(self) -> Transcript:
        from core.translation.transcript import with_phrase

        result = self._translation
        for seg_id, text in self.changes().items():
            result = with_phrase(result, seg_id, text)
        return result

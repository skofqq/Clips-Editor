"""Voices row of the Metadata tab: who each speaker of the clip is.

One line per speaker found by "Find speakers": a colour dot, how long they
talk, and a list to pick the library person. Recognised voices come preset
("recognised by voice, 72 %"); picking a person teaches the app that voice,
so the next clip recognises them by itself.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QInputDialog, QLabel, QPushButton, QVBoxLayout, QWidget

from ui.theme import palette

NEW = "\0new"


class VoicesBox(QWidget):
    assigned = Signal(int, str)  # speaker, person id ("" = nobody)
    create_requested = Signal(int, str)  # speaker, new participant's name
    find_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self.combos: dict[int, QComboBox] = {}
        self.notes: dict[int, QLabel] = {}
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(4)
        self._layout.addLayout(self.rows)
        bottom = QHBoxLayout()
        self.hint = QLabel(self)
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        self.find_button = QPushButton("Find voices", self)
        self.find_button.setToolTip("Find who speaks in the clip (Transcript → Find speakers) and recognise "
                                    "voices you named before. They become participants.")
        bottom.addWidget(self.hint, 1)
        bottom.addWidget(self.find_button)
        self._layout.addLayout(bottom)
        self.find_button.clicked.connect(self.find_requested)
        self.set_voices(None, [])

    def set_voices(self, clip, people) -> None:
        """``clip``: core.metadata.voices.ClipVoices or None; ``people``: Person profiles to offer."""
        self._loading = True
        try:
            while self.rows.count():
                item = self.rows.takeAt(0)
                layout = item.layout()
                if layout is not None:
                    while layout.count():
                        w = layout.takeAt(0).widget()
                        if w is not None:
                            w.deleteLater()
            self.combos.clear()
            self.notes.clear()
            voices = clip.voices if clip is not None else {}
            for speaker in sorted(voices):
                voice = voices[speaker]
                row = QHBoxLayout()
                dot = QLabel("●", self)
                dot.setStyleSheet(f"color: {palette.speaker_color(speaker)}")
                name = QLabel(f"Speaker {speaker + 1}" + (f" · {voice.seconds:.0f} s" if voice.seconds else ""), self)
                combo = QComboBox(self)
                combo.addItem("— not named —", "")
                for person in people:
                    combo.addItem(person.name, person.id)
                combo.addItem("New participant…", NEW)
                index = combo.findData(voice.person_id) if voice.person_id else 0
                combo.setCurrentIndex(max(0, index))
                combo.setProperty("person", voice.person_id)
                note = QLabel(self)
                note.setObjectName("muted")
                if voice.person_id and voice.auto:
                    note.setText(f"recognised by voice, {voice.similarity * 100:.0f} %")
                elif voice.windows < 3:
                    note.setText("too short to learn")
                combo.activated.connect(lambda _i, s=speaker, c=combo: self._chosen(s, c))
                row.addWidget(dot)
                row.addWidget(name)
                row.addWidget(combo, 1)
                row.addWidget(note)
                self.rows.addLayout(row)
                self.combos[speaker] = combo
                self.notes[speaker] = note
            if voices:
                self.hint.setText("Name a voice once and the next clips recognise it by themselves.")
                self.find_button.setText("Find again")
            else:
                self.hint.setText("Find who speaks: voices you named before become participants by themselves.")
                self.find_button.setText("Find voices")
        finally:
            self._loading = False

    def set_busy(self, busy: bool) -> None:
        self.find_button.setEnabled(not busy)

    def ask_name(self) -> str:
        """Name of a new participant; separate so tests can answer."""
        text, ok = QInputDialog.getText(self, "New participant", "Name or Twitch login:")
        return text.strip() if ok else ""

    def _chosen(self, speaker: int, combo: QComboBox) -> None:
        if self._loading:
            return
        value = combo.currentData()
        if value == NEW:
            name = self.ask_name()
            if name:
                self.create_requested.emit(speaker, name)
            else:  # cancelled: back to what it was
                combo.setCurrentIndex(max(0, combo.findData(combo.property("person") or "")))
            return
        combo.setProperty("person", value or "")
        note = self.notes.get(speaker)
        if note is not None:
            note.setText("")
        self.assigned.emit(speaker, value or "")

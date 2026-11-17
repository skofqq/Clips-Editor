"""Shared state for the transcript of the current clip."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.transcription.model import Transcript


class TranscriptController(QObject):
    changed = Signal()
    corrected = Signal(str, str)  # old text, new text of a hand correction

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._transcript: Transcript | None = None
        self.last_change_key: object = None

    @property
    def transcript(self) -> Transcript | None:
        return self._transcript

    def set_transcript(self, transcript: Transcript | None, key: object = None) -> bool:
        if transcript == self._transcript:
            return False
        self._transcript = transcript
        self.last_change_key = key
        self.changed.emit()
        return True

    def edit_segment_text(self, segment_id: int, text: str) -> bool:
        t = self._transcript
        seg = t.segment(segment_id) if t is not None else None
        if seg is None or not self.set_transcript(t.with_segment_text(segment_id, text)):
            return False
        self.corrected.emit(seg.text, text)
        return True

    def edit_word_text(self, segment_id: int, index: int, text: str) -> bool:
        t = self._transcript
        seg = t.segment(segment_id) if t is not None else None
        if seg is None or not 0 <= index < len(seg.words):
            return False
        old = seg.words[index].text
        if not self.set_transcript(t.with_word_text(segment_id, index, text)):
            return False
        self.corrected.emit(old, text)
        return True

"""Shared state for the censor review of the current clip."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.censor.review import CensorState


class CensorController(QObject):
    changed = Signal()  # the CensorState value changed
    current_changed = Signal()  # the suggestion highlighted in the panel changed

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._state: CensorState | None = None
        self._current: int | None = None
        self.duration_us = 0
        self.last_change_key: object = None

    @property
    def state(self) -> CensorState | None:
        return self._state

    @property
    def current(self) -> int | None:
        return self._current

    def set_state(self, state: CensorState | None, key: object = None) -> bool:
        if state == self._state:
            return False
        self._state = state
        self.last_change_key = key
        if self._current is not None and (state is None or state.get(self._current) is None):
            self._current = None
            self.current_changed.emit()
        self.changed.emit()
        return True

    def set_current(self, suggestion_id: int | None) -> None:
        if suggestion_id != self._current:
            self._current = suggestion_id
            self.current_changed.emit()

    # ---------------------------------------------------------- review
    def set_status(self, ids, status: str) -> bool:
        s = self._state
        return s is not None and self.set_state(s.with_status(ids, status))

    def set_all_status(self, status: str, only: str | None = None) -> bool:
        s = self._state
        return s is not None and self.set_state(s.with_all_status(status, only))

    def set_range(self, suggestion_id: int, start_us: int | None = None, end_us: int | None = None) -> bool:
        s = self._state
        if s is None:
            return False
        updated = s.with_range(suggestion_id, start_us, end_us, self.duration_us)
        return self.set_state(updated, key=("censor_range", suggestion_id))

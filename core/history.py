"""Snapshot-based undo/redo for immutable state values."""

from __future__ import annotations

import time
from typing import Generic, TypeVar

T = TypeVar("T")


class History(Generic[T]):
    """Keeps past states. Rapid changes that share a ``key`` (for example
    every mouse-move of one drag) are folded into a single undo step."""

    def __init__(self, initial: T, limit: int = 200, merge_window_s: float = 0.6) -> None:
        self._undo: list[T] = []
        self._redo: list[T] = []
        self._current = initial
        self._limit = limit
        self._window = merge_window_s
        self._last_key: object = None
        self._last_time = 0.0

    @property
    def current(self) -> T:
        return self._current

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def reset(self, state: T) -> None:
        self._undo.clear()
        self._redo.clear()
        self._current = state
        self._last_key = None

    def record(self, state: T, key: object = None, now: float | None = None) -> bool:
        """Store a new state; returns False when nothing changed."""
        if state == self._current:
            return False
        now = time.monotonic() if now is None else now
        merge = (
            key is not None
            and key == self._last_key
            and now - self._last_time <= self._window
            and bool(self._undo)
        )
        if not merge:
            self._undo.append(self._current)
            del self._undo[: max(0, len(self._undo) - self._limit)]
        self._current = state
        self._redo.clear()
        self._last_key = key
        self._last_time = now
        return True

    def break_merge(self) -> None:
        """Make the next change a separate undo step (e.g. on mouse release)."""
        self._last_key = None

    def undo(self) -> T | None:
        if not self._undo:
            return None
        self._redo.append(self._current)
        self._current = self._undo.pop()
        self._last_key = None
        return self._current

    def redo(self) -> T | None:
        if not self._redo:
            return None
        self._undo.append(self._current)
        self._current = self._redo.pop()
        self._last_key = None
        return self._current

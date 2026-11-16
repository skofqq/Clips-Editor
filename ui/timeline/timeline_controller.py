"""Shared editing state for the timeline (model, selection, playhead)."""

from __future__ import annotations

from fractions import Fraction

from PySide6.QtCore import QObject, Signal

from core.timeline.model import BEEP, MUTE_MODES, SILENCE, Timeline
from core.timeline.timebase import snap_to_frame

SEGMENT = "segment"
MUTE = "mute"


class TimelineController(QObject):
    changed = Signal()  # the Timeline value changed
    selection_changed = Signal()  # selected item or range changed
    playhead_changed = Signal(int)  # microseconds

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timeline: Timeline | None = None
        self._selected: tuple[str, int] | None = None
        self._range: tuple[int, int] | None = None
        self._playhead_us = 0
        self.fps = Fraction(0)
        self.default_mute_mode = SILENCE
        # Identifies the kind of the last change so that the undo history
        # can fold a continuous drag into one step. None = discrete edit.
        self.last_change_key: object = None

    # --------------------------------------------------------------- state
    @property
    def timeline(self) -> Timeline | None:
        return self._timeline

    @property
    def selected(self) -> tuple[str, int] | None:
        return self._selected

    @property
    def range(self) -> tuple[int, int] | None:
        return self._range

    @property
    def playhead_us(self) -> int:
        return self._playhead_us

    def set_timeline(self, timeline: Timeline | None, key: object = None) -> bool:
        if timeline == self._timeline:
            return False
        self._timeline = timeline
        self.last_change_key = key
        self._validate_selection()
        self.changed.emit()
        return True

    def set_playhead(self, us: int) -> None:
        us = max(0, int(us))
        if us != self._playhead_us:
            self._playhead_us = us
            self.playhead_changed.emit(us)

    def select(self, kind: str | None, ident: int | None = None) -> None:
        selected = (kind, ident) if kind is not None and ident is not None else None
        if selected != self._selected:
            self._selected = selected
            self.selection_changed.emit()

    def set_range(self, start_us: int | None, end_us: int | None = None) -> None:
        new = None
        if start_us is not None and end_us is not None and start_us != end_us:
            new = (min(start_us, end_us), max(start_us, end_us))
        if new != self._range:
            self._range = new
            self.selection_changed.emit()

    def _validate_selection(self) -> None:
        t, sel = self._timeline, self._selected
        valid = (
            t is not None
            and sel is not None
            and (
                (sel[0] == SEGMENT and 0 <= sel[1] < len(t.segments))
                or (sel[0] == MUTE and t.mute(sel[1]) is not None)
            )
        )
        if sel is not None and not valid:
            self._selected = None
            self.selection_changed.emit()
        if t is None and self._range is not None:
            self._range = None
            self.selection_changed.emit()

    def frame_snap(self, us: int) -> int:
        return snap_to_frame(us, self.fps) if self.fps else us

    # ------------------------------------------------------------- actions
    def split_at_playhead(self) -> bool:
        t = self._timeline
        return t is not None and self.set_timeline(t.split_at(self.frame_snap(self._playhead_us)))

    def delete_selection(self) -> bool:
        """Delete the selected mute region, else cut the selected range,
        else remove the selected segment."""
        t = self._timeline
        if t is None:
            return False
        if self._selected and self._selected[0] == MUTE:
            return self.set_timeline(t.delete_mute(self._selected[1]))
        if self._range:
            done = self.set_timeline(t.delete_range(*self._range))
            if done:
                self.set_range(None)
            return done
        if self._selected and self._selected[0] == SEGMENT:
            done = self.set_timeline(t.delete_segment(self._selected[1]))
            if done:
                self.select(None)  # the index now points at a different segment
            return done
        return False

    def trim_to_range(self) -> bool:
        t = self._timeline
        if t is None or not self._range:
            return False
        done = self.set_timeline(t.keep_range(*self._range))
        if done:
            self.set_range(None)
        return done

    def restore_all(self) -> bool:
        t = self._timeline
        return t is not None and self.set_timeline(t.restore_all())

    def mute_range(self, mode: str | None = None) -> bool:
        """Create a Mute Region from the selected range."""
        t = self._timeline
        if t is None or not self._range:
            return False
        updated = t.add_mute(*self._range, mode or self.default_mute_mode)
        if not self.set_timeline(updated):
            return False
        new_id = updated.next_id - 1
        self.set_range(None)
        self.select(MUTE, new_id)
        return True

    def set_mute_mode(self, mode: str) -> None:
        """Change the selected region's mode and the default for new ones."""
        if mode not in MUTE_MODES:
            return
        self.default_mute_mode = mode
        t = self._timeline
        if t is not None and self._selected and self._selected[0] == MUTE:
            self.set_timeline(t.update_mute(self._selected[1], mode=mode))

    def selected_mute_mode(self) -> str:
        t = self._timeline
        if t is not None and self._selected and self._selected[0] == MUTE:
            region = t.mute(self._selected[1])
            if region is not None:
                return region.mode
        return self.default_mute_mode

    def merge_mutes(self) -> bool:
        t = self._timeline
        return t is not None and self.set_timeline(t.merge_mutes())

    def set_gain_db(self, gain_db: float) -> None:
        if self._timeline is not None:
            self.set_timeline(self._timeline.with_gain_db(gain_db), key="gain")

    def set_fades(self, fade_in_us: int | None = None, fade_out_us: int | None = None) -> None:
        if self._timeline is not None:
            self.set_timeline(self._timeline.with_fades(fade_in_us, fade_out_us), key="fades")


__all__ = ["TimelineController", "SEGMENT", "MUTE", "SILENCE", "BEEP"]

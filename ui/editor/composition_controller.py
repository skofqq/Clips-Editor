"""Shared editing state for the composition.

The source view, output view and properties panel all read and change
the composition through this object and refresh on its signals.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from core.editor.composition import (
    REGION_NAMES,
    WEBCAM,
    Composition,
    Region,
    default_composition,
    match_slot_aspect,
)
from core.editor.geometry import Rect

MIN_CROP_PX = 16


class CompositionController(QObject):
    changed = Signal()  # composition or source size changed
    selection_changed = Signal(str)
    time_changed = Signal()  # the source time shown moved (tracked crops move with it)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._source_w = 1920
        self._source_h = 1080
        self._composition = default_composition(self._source_w, self._source_h)
        self._selected = WEBCAM
        self._time_us = 0

    # --------------------------------------------------------------- state
    @property
    def composition(self) -> Composition:
        return self._composition

    @property
    def time_us(self) -> int:
        return self._time_us

    def set_time(self, source_us: int) -> None:
        """Source time under the playhead; only matters for tracked crops."""
        source_us = int(source_us)
        if source_us != self._time_us:
            self._time_us = source_us
            if self._composition.has_tracks:
                self.time_changed.emit()

    @property
    def shown(self) -> Composition:
        """The composition as it looks at the current time."""
        return self._composition.at(self._time_us)

    @property
    def source_size(self) -> tuple[int, int]:
        return self._source_w, self._source_h

    @property
    def selected(self) -> str:
        return self._selected

    def set_composition(self, composition: Composition) -> None:
        if composition != self._composition:
            self._composition = composition
            self.changed.emit()

    def set_source_size(self, width: int, height: int) -> None:
        if width > 0 and height > 0 and (width, height) != self.source_size:
            self._source_w, self._source_h = int(width), int(height)
            self.changed.emit()

    def select(self, name: str) -> None:
        if name in REGION_NAMES and name != self._selected:
            self._selected = name
            self.selection_changed.emit(name)

    # ------------------------------------------------------------- regions
    def region(self, name: str | None = None) -> Region:
        return self._composition.region(name or self._selected)

    def update_region(self, name: str, **changes) -> None:
        region = replace(self._composition.region(name), **changes)
        self.set_composition(self._composition.with_region(name, region))

    def crop_px(self, name: str) -> Rect:
        """The crop as shown now (a tracked crop is where the track puts it)."""
        return self.shown.crop_px(name, self._source_w, self._source_h)

    def set_crop_px(self, name: str, rect: Rect) -> None:
        """Set a crop in source pixels; clamped to the frame. A tracked crop
        keeps its motion: the whole path moves with the edit."""
        bounds = Rect(0, 0, self._source_w, self._source_h)
        rect = Rect(rect.x, rect.y, max(rect.w, MIN_CROP_PX), max(rect.h, MIN_CROP_PX))
        rect = rect.moved_inside(bounds)
        crop = rect.scaled(1 / self._source_w, 1 / self._source_h)
        region = self._composition.region(name)
        if region.track:
            shown = region.at(self._time_us).crop
            # Undo the part of the offset the track adds at this moment.
            crop = Rect(crop.x - (shown.x - region.crop.x), crop.y - (shown.y - region.crop.y), crop.w, crop.h)
            crop = crop.moved_inside(Rect(0, 0, 1, 1))
        self.update_region(name, crop=crop)

    def set_track(self, name: str, crop: Rect, track) -> None:
        """Auto-framing result: a crop (normalised) and its motion."""
        self.update_region(name, crop=crop, track=tuple(track))

    def clear_track(self, name: str) -> None:
        region = self._composition.region(name)
        if region.track:
            self.update_region(name, crop=region.at(self._time_us).crop, track=())

    # --------------------------------------------------------- composition
    def set_divider(self, divider: float) -> None:
        self.set_composition(self._composition.with_divider(divider))

    def swap(self) -> None:
        self.set_composition(self._composition.with_swapped())

    def match_aspect(self, name: str) -> None:
        self.set_composition(
            match_slot_aspect(self._composition, name, self._source_w, self._source_h)
        )

    def reset_placement(self, name: str) -> None:
        self.update_region(name, scale=1.0, offset_x=0.0, offset_y=0.0)

    def reset(self) -> None:
        self.set_composition(default_composition(self._source_w, self._source_h))

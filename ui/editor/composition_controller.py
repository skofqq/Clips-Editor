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
from core.editor.track import MAX_ZOOM, MIN_ZOOM, TrackKey, clean_track, has_zoom, offset_at, zoom_at

MIN_CROP_PX = 16
KEY_SNAP_US = 100_000  # the playhead this close to a key edits that key


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
        self._pending: dict[str, TrackKey] = {}  # a first key without motion yet (kept until the next one)
        self._keyed: set[str] = set()  # regions keyed by hand: a drag at a key edits that key

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
            if self._composition.has_tracks or self._pending:
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
        """A composition from outside (another clip or part, undo): hand-made key state starts over."""
        self._pending.clear()
        self._keyed = {name for name in self._keyed if composition.region(name).track}
        self._set(composition)

    def _set(self, composition: Composition) -> None:
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
        self._set(self._composition.with_region(name, region))

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
        key = self.key_at(name) if self._edits_keys(name) else None
        if key is not None:  # at a keyframe: the drag pans (and a resize zooms) that key
            base = region.crop
            zoom = max(MIN_ZOOM, min(MAX_ZOOM, base.w / max(crop.w, 1e-6)))
            dx = (crop.x + crop.w / 2) - (base.x + base.w / 2)
            dy = (crop.y + crop.h / 2) - (base.y + base.h / 2)
            self._put_key(name, TrackKey(key.t_us, dx, dy, zoom))
            return
        if region.track:
            shown = region.at(self._time_us).crop
            # Undo the part of the offset the track adds at this moment.
            crop = Rect(crop.x - (shown.x - region.crop.x), crop.y - (shown.y - region.crop.y), crop.w, crop.h)
            crop = crop.moved_inside(Rect(0, 0, 1, 1))
        self.update_region(name, crop=crop)

    def _edits_keys(self, name: str) -> bool:
        """Hand-made keys (or any zoom) are edited one by one; a face-following
        path is moved as a whole."""
        return name in self._keyed or name in self._pending or has_zoom(self._composition.region(name).track)

    def set_track(self, name: str, crop: Rect, track) -> None:
        """Auto-framing result: a crop (normalised) and its motion."""
        self._keyed.discard(name)
        self._pending.pop(name, None)
        self.update_region(name, crop=crop, track=tuple(track))

    # ------------------------------------------------------------ keyframes
    def keys(self, name: str) -> tuple[TrackKey, ...]:
        track = self._composition.region(name).track
        pending = self._pending.get(name)
        return tuple(sorted(track + ((pending,) if pending is not None and pending not in track else ())))

    def key_at(self, name: str, t_us: int | None = None) -> TrackKey | None:
        t = self._time_us if t_us is None else t_us
        near = [k for k in self.keys(name) if abs(k.t_us - t) <= KEY_SNAP_US]
        return min(near, key=lambda k: abs(k.t_us - t)) if near else None

    def zoom(self, name: str) -> float:
        return zoom_at(self.keys(name), self._time_us)

    def add_key(self, name: str, zoom: float | None = None) -> TrackKey:
        """A key at the playhead holding the motion and zoom of this moment (or ``zoom``)."""
        self._keyed.add(name)
        keys = self.keys(name)
        existing = self.key_at(name)
        dx, dy = offset_at(keys, self._time_us)
        z = zoom_at(keys, self._time_us) if zoom is None else zoom
        if existing is not None:
            key = TrackKey(existing.t_us, existing.dx, existing.dy, z if zoom is not None else existing.z)
        else:
            key = TrackKey(self._time_us, dx, dy, z)
        self._put_key(name, key)
        return key

    def remove_key(self, name: str) -> bool:
        key = self.key_at(name)
        if key is None:
            return False
        rest = [k for k in self.keys(name) if k != key]
        track = clean_track(rest)
        self._pending.pop(name, None)
        if not track and len(rest) == 1:  # the last key stays, waiting for motion again
            self._pending[name] = rest[0]
        self.update_region(name, track=track)
        self.changed.emit()
        return True

    def _put_key(self, name: str, key: TrackKey) -> None:
        keys = [k for k in self.keys(name) if k.t_us != key.t_us] + [key]
        track = clean_track(keys)
        if not track:  # no motion yet: remember the key until the next one gives it a meaning
            self._pending[name] = TrackKey(key.t_us, key.dx, key.dy, key.z)
            self.update_region(name, track=())
            self.changed.emit()
            return
        self._pending.pop(name, None)
        self.update_region(name, track=track)

    def clear_track(self, name: str) -> None:
        self._pending.pop(name, None)
        self._keyed.discard(name)
        region = self._composition.region(name)
        if region.track:
            zoomed = any(k.z != 1.0 for k in region.track)
            # Face motion: keep the crop where it is now. Zoom keys: back to the region's own crop.
            self.update_region(name, crop=region.crop if zoomed else region.at(self._time_us).crop, track=())

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

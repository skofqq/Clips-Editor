"""Stacked Layout composition model.

A :class:`Composition` describes how two crops of the source frame
(webcam and gameplay) are placed on the vertical output canvas.

Crops are stored **normalised** (0..1 of the source frame) so a preset
works for any source resolution. :meth:`Composition.layout` converts the
model into concrete pixel rectangles; the preview painter and (in Phase 6)
the FFmpeg filter-graph builder both consume that result, so the preview
matches the render by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from core.editor.geometry import UNIT, Rect, clamp
from core.editor.track import TrackKey, clean_track, offset_at, zoom_at

WEBCAM = "webcam"
GAMEPLAY = "gameplay"
REGION_NAMES = (WEBCAM, GAMEPLAY)

MIN_CROP = 0.02  # smallest crop side, as a fraction of the source
MIN_SCALE, MAX_SCALE = 0.25, 8.0
MIN_DIVIDER, MAX_DIVIDER = 0.1, 0.9


class FitMode(str, Enum):
    FILL = "fill"  # cover the slot, cropping overflow
    FIT = "fit"  # show the whole crop, leaving bars


def normalise_crop(crop: Rect) -> Rect:
    """Clamp a normalised crop to the unit square with a minimum size."""
    w = clamp(crop.w, MIN_CROP, 1.0)
    h = clamp(crop.h, MIN_CROP, 1.0)
    fitted = Rect(crop.x, crop.y, w, h).moved_inside(UNIT)
    # Stored precision: what is in memory is exactly what a project file holds.
    return Rect(*(round(v, 6) for v in (fitted.x, fitted.y, fitted.w, fitted.h)))


@dataclass(frozen=True)
class Region:
    crop: Rect
    enabled: bool = True
    fit: FitMode = FitMode.FILL
    scale: float = 1.0  # extra zoom on top of fit/fill
    offset_x: float = 0.0  # -1..1, position of the content inside its slot
    offset_y: float = 0.0
    track: tuple[TrackKey, ...] = ()  # crop motion over source time (face following)

    def at(self, t_us: int) -> "Region":
        """The region with its crop where the track puts it at ``t_us``."""
        if not self.track:
            return self
        dx, dy = offset_at(self.track, t_us)
        moved = Rect(self.crop.x + dx, self.crop.y + dy, self.crop.w, self.crop.h).moved_inside(UNIT)
        zoom = zoom_at(self.track, t_us)
        if zoom != 1.0:  # closer: a smaller crop around the same centre
            cx, cy = moved.x + moved.w / 2, moved.y + moved.h / 2
            w, h = moved.w / zoom, moved.h / zoom
            moved = Rect(cx - w / 2, cy - h / 2, w, h)
        return replace(self, crop=moved, track=())

    def cleaned(self) -> "Region":
        return replace(
            self,
            crop=normalise_crop(self.crop),
            track=clean_track(self.track),
            fit=FitMode(self.fit),
            scale=round(clamp(self.scale, MIN_SCALE, MAX_SCALE), 4),
            offset_x=round(clamp(self.offset_x, -1.0, 1.0), 4),
            offset_y=round(clamp(self.offset_y, -1.0, 1.0), 4),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "crop": self.crop.to_list(),
            "enabled": self.enabled,
            "fit": self.fit.value,
            "scale": self.scale,
            "offset": [self.offset_x, self.offset_y],
            **({"track": [k.to_list() for k in self.track]} if self.track else {}),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Region":
        if not isinstance(data, dict):
            raise ValueError("Region must be an object.")
        offset = data.get("offset") or [0.0, 0.0]
        if not isinstance(offset, (list, tuple)) or len(offset) != 2:
            raise ValueError("Region offset must be two numbers.")
        try:
            fit = FitMode(data.get("fit", FitMode.FILL.value))
        except ValueError as exc:
            raise ValueError(f"Unknown fit mode: {data.get('fit')!r}") from exc
        return cls(
            crop=Rect.from_list(data.get("crop")),
            enabled=bool(data.get("enabled", True)),
            fit=fit,
            scale=float(data.get("scale", 1.0)),
            offset_x=float(offset[0]),
            offset_y=float(offset[1]),
            track=tuple(TrackKey.from_list(k) for k in data.get("track") or ()),
        ).cleaned()


@dataclass(frozen=True)
class Placement:
    """One region resolved to pixels.

    ``src`` is the visible part of the source frame (source pixels);
    ``dst`` is where it lands on the output canvas (output pixels);
    ``slot`` is the area of the canvas reserved for the region.
    """

    name: str
    slot: Rect
    src: Rect
    dst: Rect


@dataclass(frozen=True)
class Composition:
    output_w: int = 1080
    output_h: int = 1920
    divider: float = 0.35  # share of the canvas height given to the top slot
    swapped: bool = False  # False: webcam on top, gameplay below
    webcam: Region = field(default_factory=lambda: Region(Rect(0.0, 0.0, 0.3, 0.3)))
    gameplay: Region = field(default_factory=lambda: Region(Rect(0.25, 0.0, 0.5, 1.0)))

    # ------------------------------------------------------------ access
    def region(self, name: str) -> Region:
        if name not in REGION_NAMES:
            raise KeyError(name)
        return getattr(self, name)

    def with_region(self, name: str, region: Region) -> "Composition":
        if name not in REGION_NAMES:
            raise KeyError(name)
        return replace(self, **{name: region.cleaned()})

    def with_divider(self, divider: float) -> "Composition":
        return replace(self, divider=round(clamp(divider, MIN_DIVIDER, MAX_DIVIDER), 4))

    def with_swapped(self) -> "Composition":
        return replace(self, swapped=not self.swapped)

    @property
    def has_tracks(self) -> bool:
        return bool(self.webcam.track or self.gameplay.track)

    def at(self, t_us: int) -> "Composition":
        """Static composition for source time ``t_us`` (tracks applied)."""
        if not self.has_tracks:
            return self
        return replace(self, webcam=self.webcam.at(t_us), gameplay=self.gameplay.at(t_us))

    def without_tracks(self) -> "Composition":
        if not self.has_tracks:
            return self
        return replace(self, webcam=replace(self.webcam, track=()), gameplay=replace(self.gameplay, track=()))

    @property
    def top_name(self) -> str:
        return GAMEPLAY if self.swapped else WEBCAM

    # ------------------------------------------------------------ layout
    def slots(self) -> dict[str, Rect]:
        """Canvas area of each *enabled* region, in output pixels."""
        w, h = float(self.output_w), float(self.output_h)
        enabled = [n for n in REGION_NAMES if self.region(n).enabled]
        if not enabled:
            return {}
        if len(enabled) == 1:
            return {enabled[0]: Rect(0.0, 0.0, w, h)}
        split = round(h * clamp(self.divider, MIN_DIVIDER, MAX_DIVIDER))
        top, bottom = self.top_name, (WEBCAM if self.swapped else GAMEPLAY)
        return {top: Rect(0.0, 0.0, w, split), bottom: Rect(0.0, split, w, h - split)}

    def crop_px(self, name: str, source_w: float, source_h: float) -> Rect:
        return self.region(name).crop.scaled(source_w, source_h)

    def content_rect(self, name: str, source_w: float, source_h: float) -> Rect | None:
        """Where the whole (unclipped) crop would land on the canvas."""
        slot = self.slots().get(name)
        if slot is None:
            return None
        region = self.region(name)
        crop = self.crop_px(name, source_w, source_h)
        if crop.is_empty or slot.is_empty:
            return None
        ratios = (slot.w / crop.w, slot.h / crop.h)
        base = max(ratios) if region.fit is FitMode.FILL else min(ratios)
        scale = base * region.scale
        cw, ch = crop.w * scale, crop.h * scale
        free_x, free_y = slot.w - cw, slot.h - ch
        x = slot.x + free_x / 2 + region.offset_x * abs(free_x) / 2
        y = slot.y + free_y / 2 + region.offset_y * abs(free_y) / 2
        return Rect(x, y, cw, ch)

    def layout(self, source_w: float, source_h: float) -> list[Placement]:
        """Resolve enabled regions to pixel rectangles, in draw order."""
        result: list[Placement] = []
        slots = self.slots()
        for name in REGION_NAMES:
            slot = slots.get(name)
            content = self.content_rect(name, source_w, source_h)
            if slot is None or content is None:
                continue
            dst = content.intersected(slot)
            if dst.is_empty:
                continue
            crop = self.crop_px(name, source_w, source_h)
            # Map the visible part of the content back into the source crop.
            kx, ky = crop.w / content.w, crop.h / content.h
            src = Rect(
                crop.x + (dst.x - content.x) * kx,
                crop.y + (dst.y - content.y) * ky,
                dst.w * kx,
                dst.h * ky,
            )
            result.append(Placement(name=name, slot=slot, src=src, dst=dst))
        return result

    # ----------------------------------------------------- serialisation
    def to_dict(self) -> dict[str, Any]:
        return {
            "layout": "stacked",
            "output": [self.output_w, self.output_h],
            "divider": self.divider,
            "swapped": self.swapped,
            WEBCAM: self.webcam.to_dict(),
            GAMEPLAY: self.gameplay.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Composition":
        if not isinstance(data, dict):
            raise ValueError("Composition must be an object.")
        output = data.get("output") or [1080, 1920]
        if not isinstance(output, (list, tuple)) or len(output) != 2:
            raise ValueError("Composition output must be [width, height].")
        out_w, out_h = int(output[0]), int(output[1])
        if out_w < 16 or out_h < 16:
            raise ValueError("Composition output size is too small.")
        return cls(
            output_w=out_w,
            output_h=out_h,
            divider=round(clamp(float(data.get("divider", 0.35)), MIN_DIVIDER, MAX_DIVIDER), 4),
            swapped=bool(data.get("swapped", False)),
            webcam=Region.from_dict(data.get(WEBCAM)),
            gameplay=Region.from_dict(data.get(GAMEPLAY)),
        )


# ------------------------------------------------------------------ helpers

def match_slot_aspect(comp: Composition, name: str, source_w: float, source_h: float) -> Composition:
    """Resize a crop around its centre so its aspect equals its slot's."""
    slot = comp.slots().get(name)
    if slot is None or slot.is_empty or source_w <= 0 or source_h <= 0:
        return comp
    aspect = slot.w / slot.h
    crop = comp.crop_px(name, source_w, source_h)
    cx, cy = crop.center
    w, h = crop.w, crop.w / aspect
    if h > source_h:
        h = source_h
        w = h * aspect
    if w > source_w:
        w = source_w
        h = w / aspect
    fitted = Rect(cx - w / 2, cy - h / 2, w, h).moved_inside(Rect(0, 0, source_w, source_h))
    region = replace(comp.region(name), crop=fitted.scaled(1 / source_w, 1 / source_h))
    return comp.with_region(name, region)


def default_composition(
    source_w: float = 1920, source_h: float = 1080, webcam_corner: str = "top-left"
) -> Composition:
    """Sensible starting point: webcam from a corner, gameplay from the centre."""
    comp = Composition()
    wx = 0.0 if "left" in webcam_corner else 0.7
    wy = 0.0 if "top" in webcam_corner else 0.7
    comp = comp.with_region(WEBCAM, Region(Rect(wx, wy, 0.3, 0.3)))
    comp = comp.with_region(GAMEPLAY, Region(Rect(0.0, 0.0, 1.0, 1.0)))
    if source_w > 0 and source_h > 0:
        comp = match_slot_aspect(comp, WEBCAM, source_w, source_h)
        comp = match_slot_aspect(comp, GAMEPLAY, source_w, source_h)
    return comp

"""Blur boxes: a rectangle of the source frame blurred (or pixelated) for a
stretch of time, with soft edges, optionally following a moving object.

Everything is in **source** coordinates and time, like mute regions: the box
is applied to the source frame before the crops and the layout, so it stays
on the object whatever the layout, zoom or cuts do. The rectangle is
normalised (0..1 of the source size). A ``track`` moves it over time with
the same offset keys as face following (:mod:`core.editor.track`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from core.editor.geometry import Rect
from core.editor.track import TrackKey, clean_track, offset_at

BLUR, PIXELATE = "blur", "pixelate"
BLUR_MODES = (BLUR, PIXELATE)
MIN_BLUR_US = 100_000
MIN_SIZE = 0.01  # share of the source side
DEFAULT_STRENGTH = 50
DEFAULT_FEATHER = 30


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class BlurRegion:
    id: int
    start_us: int
    end_us: int
    rect: Rect  # normalised, at the region's own position (offsets move it)
    strength: int = DEFAULT_STRENGTH  # 1..100
    feather: int = DEFAULT_FEATHER  # 0..100: soft edge, share of half the smaller side
    mode: str = BLUR
    track: tuple[TrackKey, ...] = field(default=())

    @property
    def length_us(self) -> int:
        return self.end_us - self.start_us

    def active(self, t_us: int) -> bool:
        return self.start_us <= t_us < self.end_us

    def rect_at(self, t_us: int) -> Rect:
        """Normalised rectangle at source time ``t_us`` (kept inside the frame)."""
        dx, dy = offset_at(self.track, t_us)
        r = self.rect
        return Rect(_clamp(r.x + dx, 0.0, 1.0 - r.w), _clamp(r.y + dy, 0.0, 1.0 - r.h), r.w, r.h)

    # ------------------------------------------------- render parameters
    def sigma_px(self, src_h: int) -> float:
        """Gaussian blur radius in source pixels (100 → 40 px on a 1080p frame)."""
        return max(0.5, self.strength * 0.4 * src_h / 1080)

    def block_px(self, src_h: int) -> int:
        """Pixel block size for Pixelate (100 → 60 px on a 1080p frame)."""
        return max(2, int(round(self.strength * 0.6 * src_h / 1080)))

    def feather_px(self, w_px: float, h_px: float) -> float:
        return self.feather / 100 * min(w_px, h_px) / 2

    # ------------------------------------------------------ serialisation
    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id, "start_us": self.start_us, "end_us": self.end_us,
            "rect": [self.rect.x, self.rect.y, self.rect.w, self.rect.h],
            "strength": self.strength, "feather": self.feather, "mode": self.mode,
        }
        if self.track:
            data["track"] = [k.to_list() for k in self.track]
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "BlurRegion":
        if not isinstance(data, dict):
            raise ValueError("Blur must be an object.")
        x, y, w, h = (float(v) for v in data["rect"])
        region = cls(
            id=int(data["id"]), start_us=int(data["start_us"]), end_us=int(data["end_us"]),
            rect=Rect(x, y, w, h), strength=int(data.get("strength", DEFAULT_STRENGTH)),
            feather=int(data.get("feather", DEFAULT_FEATHER)), mode=str(data.get("mode", BLUR)),
            track=tuple(TrackKey.from_list(k) for k in data.get("track") or ()),
        )
        return region.cleaned()

    def cleaned(self, duration_us: int | None = None) -> "BlurRegion":
        start, end = sorted((int(self.start_us), int(self.end_us)))
        start = max(0, start)
        if duration_us is not None:
            end = min(end, duration_us)
            start = min(start, max(0, end - MIN_BLUR_US))
        end = max(end, start + MIN_BLUR_US)
        w = _clamp(self.rect.w, MIN_SIZE, 1.0)
        h = _clamp(self.rect.h, MIN_SIZE, 1.0)
        rect = Rect(round(_clamp(self.rect.x, 0.0, 1.0 - w), 6), round(_clamp(self.rect.y, 0.0, 1.0 - h), 6),
                    round(w, 6), round(h, 6))
        return replace(
            self, start_us=start, end_us=end, rect=rect,
            strength=int(_clamp(self.strength, 1, 100)), feather=int(_clamp(self.feather, 0, 100)),
            mode=self.mode if self.mode in BLUR_MODES else BLUR, track=clean_track(self.track),
        )


def default_rect(near: Rect | None = None) -> Rect:
    """A box for a new blur: centred in the frame, about a fifth of it."""
    return Rect(0.4, 0.4, 0.2, 0.2) if near is None else near


def blurs_at(blurs: tuple[BlurRegion, ...], t_us: int) -> list[BlurRegion]:
    return [b for b in blurs if b.active(t_us)]

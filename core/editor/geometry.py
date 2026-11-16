"""Small immutable rectangle type used by the composition model."""

from __future__ import annotations

from dataclasses import dataclass


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    @property
    def is_empty(self) -> bool:
        return self.w <= 0 or self.h <= 0

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.right and self.y <= py <= self.bottom

    def intersected(self, other: "Rect") -> "Rect":
        x = max(self.x, other.x)
        y = max(self.y, other.y)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        return Rect(x, y, max(0.0, right - x), max(0.0, bottom - y))

    def scaled(self, sx: float, sy: float) -> "Rect":
        return Rect(self.x * sx, self.y * sy, self.w * sx, self.h * sy)

    def moved_inside(self, bounds: "Rect") -> "Rect":
        """Same size (shrunk if larger than ``bounds``), shifted to fit inside."""
        w = min(self.w, bounds.w)
        h = min(self.h, bounds.h)
        x = clamp(self.x, bounds.x, bounds.right - w)
        y = clamp(self.y, bounds.y, bounds.bottom - h)
        return Rect(x, y, w, h)

    def to_list(self) -> list[float]:
        return [round(self.x, 6), round(self.y, 6), round(self.w, 6), round(self.h, 6)]

    @classmethod
    def from_list(cls, data) -> "Rect":
        if not isinstance(data, (list, tuple)) or len(data) != 4:
            raise ValueError("Rectangle must be a list of four numbers.")
        return cls(*(float(v) for v in data))

    @classmethod
    def from_points(cls, x1: float, y1: float, x2: float, y2: float) -> "Rect":
        return cls(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))


UNIT = Rect(0.0, 0.0, 1.0, 1.0)

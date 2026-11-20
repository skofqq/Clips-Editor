"""Motion of a crop over time: face tracking and zoom/pan keyframes.

A track is a list of keys in **source time**; each key says how far the
crop is moved from the region's own crop (normalised, like the crop) and how
much it is zoomed in (``z`` = 2 shows half the width and height, centred on
the moved crop). Values are interpolated linearly between keys and held
before the first and after the last one. Storing offsets keeps the motion
when the user later drags or resizes the crop by hand.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Iterable

MAX_KEYS = 400
MIN_ZOOM, MAX_ZOOM = 1.0, 3.0


@dataclass(frozen=True, order=True)
class TrackKey:
    t_us: int
    dx: float = 0.0
    dy: float = 0.0
    z: float = 1.0  # zoom: 1 = the region's crop, 2 = twice closer

    def to_list(self) -> list:
        return [self.t_us, self.dx, self.dy] + ([self.z] if self.z != 1.0 else [])

    @classmethod
    def from_list(cls, data: Any) -> "TrackKey":
        if not isinstance(data, (list, tuple)) or len(data) < 3:
            raise ValueError("Track key must be [t_us, dx, dy] or [t_us, dx, dy, zoom].")
        zoom = float(data[3]) if len(data) > 3 else 1.0
        return cls(int(data[0]), float(data[1]), float(data[2]), zoom)


def clean_track(keys: Iterable[TrackKey]) -> tuple[TrackKey, ...]:
    """Sorted, one key per time, rounded like crops, at most MAX_KEYS."""
    by_time: dict[int, TrackKey] = {}
    for key in keys:
        t = max(0, int(key.t_us))
        by_time[t] = TrackKey(t, round(max(-1.0, min(1.0, key.dx)), 6), round(max(-1.0, min(1.0, key.dy)), 6),
                              round(max(MIN_ZOOM, min(MAX_ZOOM, key.z)), 4))
    result = tuple(sorted(by_time.values()))
    if all(k.dx == 0 and k.dy == 0 and k.z == 1.0 for k in result):
        return ()
    return result[:MAX_KEYS]


def offset_at(keys: tuple[TrackKey, ...], t_us: int) -> tuple[float, float]:
    if not keys:
        return 0.0, 0.0
    times = [k.t_us for k in keys]
    i = bisect_right(times, t_us)
    if i == 0:
        return keys[0].dx, keys[0].dy
    if i == len(keys):
        return keys[-1].dx, keys[-1].dy
    a, b = keys[i - 1], keys[i]
    f = (t_us - a.t_us) / max(1, b.t_us - a.t_us)
    return a.dx + (b.dx - a.dx) * f, a.dy + (b.dy - a.dy) * f


def zoom_at(keys: tuple[TrackKey, ...], t_us: int) -> float:
    if not keys:
        return 1.0
    times = [k.t_us for k in keys]
    i = bisect_right(times, t_us)
    if i == 0:
        return keys[0].z
    if i == len(keys):
        return keys[-1].z
    a, b = keys[i - 1], keys[i]
    f = (t_us - a.t_us) / max(1, b.t_us - a.t_us)
    return a.z + (b.z - a.z) * f


def has_zoom(keys: tuple[TrackKey, ...]) -> bool:
    return any(k.z != 1.0 for k in keys)


def piecewise_expression(points: list[tuple[float, float]], var: str = "t", digits: int = 3) -> str:
    """FFmpeg expression of a piecewise linear function of ``var`` through
    ``points`` (time in seconds, value); held flat outside them."""
    pts: list[tuple[float, float]] = []
    for t, v in sorted(points):
        if pts and abs(t - pts[-1][0]) < 1e-6:
            pts[-1] = (t, v)
        else:
            pts.append((t, v))
    if not pts:
        return "0"
    if len(pts) == 1 or all(abs(v - pts[0][1]) < 1e-9 for _, v in pts):
        return f"{pts[0][1]:.{digits}f}"
    expr = f"{pts[-1][1]:.{digits}f}"
    for i in range(len(pts) - 1, 0, -1):
        (t0, v0), (t1, v1) = pts[i - 1], pts[i]
        slope = (v1 - v0) / (t1 - t0)
        expr = f"if(lt({var},{t1:.4f}),{v0:.{digits}f}+{slope:.{digits + 1}f}*({var}-{t0:.4f}),{expr})"
    return f"if(lt({var},{pts[0][0]:.4f}),{pts[0][1]:.{digits}f},{expr})"

"""Motion of a crop over time (face tracking now, zoom/pan keyframes later).

A track is a list of keys in **source time**; each key says how far the
crop is moved from the region's own crop (normalised, like the crop). The
offset is interpolated linearly between keys and held before the first and
after the last one. Storing offsets keeps the motion when the user later
drags or resizes the crop by hand.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Iterable

MAX_KEYS = 400


@dataclass(frozen=True, order=True)
class TrackKey:
    t_us: int
    dx: float = 0.0
    dy: float = 0.0

    def to_list(self) -> list:
        return [self.t_us, self.dx, self.dy]

    @classmethod
    def from_list(cls, data: Any) -> "TrackKey":
        if not isinstance(data, (list, tuple)) or len(data) < 3:
            raise ValueError("Track key must be [t_us, dx, dy].")
        return cls(int(data[0]), float(data[1]), float(data[2]))


def clean_track(keys: Iterable[TrackKey]) -> tuple[TrackKey, ...]:
    """Sorted, one key per time, rounded like crops, at most MAX_KEYS."""
    by_time: dict[int, TrackKey] = {}
    for key in keys:
        t = max(0, int(key.t_us))
        by_time[t] = TrackKey(t, round(max(-1.0, min(1.0, key.dx)), 6), round(max(-1.0, min(1.0, key.dy)), 6))
    result = tuple(sorted(by_time.values()))
    if all(k.dx == 0 and k.dy == 0 for k in result):
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


def piecewise_expression(points: list[tuple[float, float]], var: str = "t") -> str:
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
        return f"{pts[0][1]:.3f}"
    expr = f"{pts[-1][1]:.3f}"
    for i in range(len(pts) - 1, 0, -1):
        (t0, v0), (t1, v1) = pts[i - 1], pts[i]
        slope = (v1 - v0) / (t1 - t0)
        expr = f"if(lt({var},{t1:.4f}),{v0:.3f}+{slope:.4f}*({var}-{t0:.4f}),{expr})"
    return f"if(lt({var},{pts[0][0]:.4f}),{pts[0][1]:.3f},{expr})"

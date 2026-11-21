"""When to publish: the time of day the channel's Shorts did best.

YouTube's API does not say when the audience is online, so this looks at the
channel's own Shorts: each one's views, grouped by the local hour it went out
in 3-hour windows. The window with the highest median wins when it has enough
Shorts; brand-new uploads (under two days) are left out because their views
are still climbing.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

WINDOW_H = 3
MIN_IN_WINDOW = 2
MIN_TOTAL = 5
SETTLE = timedelta(days=2)


@dataclass(frozen=True)
class BestTime:
    start_hour: int  # local hour the window starts
    median_views: int
    shorts: int  # how many Shorts went out in this window
    total: int  # how many Shorts were looked at
    overall_median: int

    @property
    def window_text(self) -> str:
        return f"{self.start_hour:02d}:00–{(self.start_hour + WINDOW_H) % 24:02d}:00"

    @property
    def lift(self) -> float:
        """How many times more views than the channel's usual Short."""
        return self.median_views / self.overall_median if self.overall_median else 1.0

    def next_slot(self, now: datetime) -> datetime:
        """The next start of the window (local time), at least 15 minutes from ``now``."""
        slot = now.replace(hour=self.start_hour, minute=0, second=0, microsecond=0)
        if slot < now + timedelta(minutes=15):
            slot += timedelta(days=1)
        return slot


def _local(published_at: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone()


def best_time(stats, now: datetime | None = None) -> BestTime | None:
    """``stats``: core.youtube.analytics.VideoStats of the channel. None when there is too little to go on."""
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    windows: dict[int, list[int]] = {}
    views_all: list[int] = []
    for s in stats:
        if not getattr(s, "is_short", True):
            continue
        moment = _local(s.published_at or "")
        if moment is None or now - moment < SETTLE:
            continue
        windows.setdefault(moment.hour // WINDOW_H * WINDOW_H, []).append(int(s.views))
        views_all.append(int(s.views))
    if len(views_all) < MIN_TOTAL:
        return None
    candidates = [(statistics.median(v), len(v), hour) for hour, v in windows.items() if len(v) >= MIN_IN_WINDOW]
    if not candidates:
        return None
    median, count, hour = max(candidates)
    return BestTime(hour, int(median), count, len(views_all), int(statistics.median(views_all)))

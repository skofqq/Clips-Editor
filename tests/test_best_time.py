from datetime import datetime, timedelta, timezone

from core.youtube.analytics import VideoStats
from core.youtube.best_time import best_time

UTC = timezone.utc


def _short(day: int, hour: int, views: int) -> VideoStats:
    at = datetime(2026, 11, day, hour, 30, tzinfo=UTC).isoformat()
    return VideoStats(f"v{day}{hour}", "", at, 40, views, 0, 0)


def test_best_window_from_the_channel_shorts():
    now = datetime(2026, 11, 21, 5, 0, tzinfo=UTC)
    stats = [_short(1, 19, 9000), _short(3, 20, 12000), _short(5, 18, 8000),  # evenings
             _short(2, 9, 900), _short(4, 10, 1200), _short(6, 11, 700),  # mornings
             _short(20, 19, 50),  # too new to count
             VideoStats("long", "", datetime(2026, 11, 7, 19, tzinfo=UTC).isoformat(), 900, 10 ** 6, 0, 0)]
    best = best_time(stats, now)
    local_evening = datetime(2026, 11, 1, 19, 30, tzinfo=UTC).astimezone().hour // 3 * 3
    assert best is not None and best.start_hour == local_evening
    assert best.shorts >= 2 and best.total == 6 and best.lift > 1
    slot = best.next_slot(now.astimezone())
    assert slot.hour == best.start_hour and slot > now and slot - now < timedelta(days=1, minutes=16)


def test_too_few_shorts_gives_no_advice():
    now = datetime(2026, 11, 21, tzinfo=UTC)
    assert best_time([_short(1, 19, 9000), _short(2, 9, 10)], now) is None

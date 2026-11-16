"""Exact time conversions.

All timeline positions are integer **microseconds** in source time.
Frame and audio-sample positions are derived on demand with rational
arithmetic, so repeated conversions never accumulate rounding drift.
"""

from __future__ import annotations

from fractions import Fraction

US = 1_000_000


def seconds_to_us(seconds: float) -> int:
    return int(round(seconds * US))


def us_to_seconds(us: int) -> float:
    return us / US


def ms_to_us(ms: int) -> int:
    return int(ms) * 1000


def us_to_ms(us: int) -> int:
    return int(round(us / 1000))


def frame_to_us(frame: int, fps: Fraction) -> int:
    """Start time of ``frame`` (rounded to the nearest microsecond)."""
    return int(round(Fraction(frame * US) / Fraction(fps)))


def us_to_frame(us: int, fps: Fraction) -> int:
    """Index of the frame being displayed at ``us``."""
    # Half a microsecond of tolerance undoes the rounding in frame_to_us.
    return int((Fraction(2 * us + 1, 2) * Fraction(fps)) // US)


def snap_to_frame(us: int, fps: Fraction) -> int:
    """Nearest frame boundary."""
    if not fps:
        return us
    return frame_to_us(int(round(Fraction(us) * Fraction(fps) / US)), fps)


def us_to_sample(us: int, sample_rate: int) -> int:
    return (us * sample_rate) // US


def sample_to_us(sample: int, sample_rate: int) -> int:
    return int(round(Fraction(sample * US, sample_rate)))


def format_timecode(us: int, show_ms: bool = True) -> str:
    us = max(0, int(us))
    total_ms = us // 1000
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, ms = divmod(rem, 1000)
    text = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
    return f"{text}.{ms:03d}" if show_ms else text

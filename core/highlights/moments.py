"""Shorts-length moments in a long clip or a stream recording.

The short-moment finder (:mod:`core.highlights.finder`) marks a few
seconds around each loud or funny spot. For a long recording the user
needs something else: ready ranges of about the length of a Short
(20–60 s) that start with the setup and end after the reaction, ranked
from best to worst.

How a range is chosen:

1. All evidence becomes events: loud spikes, laughter/shouting/hype in the
   transcript, chat bursts (with laughing or hype emotes).
2. Candidate windows of the wanted length are placed around every event
   (event at 60 % of the window, near its start, near its end).
3. A window's strength is the sum of its event scores *per kind*, each
   kind capped, so a window with a shout, laughter **and** a chat burst
   beats one with ten loud spikes.
4. The strongest windows are taken greedily without overlap, then the
   edges are moved to sentence boundaries (transcript) or to a quiet point,
   so a Short never starts or ends in the middle of a word.

Nothing is cut: the user reviews the list and ticks the moments to keep.
"""

from __future__ import annotations

import bisect
import heapq
import math
from collections import defaultdict
from typing import Iterable

import numpy as np

from core.highlights.finder import (
    HIGH,
    HYPE,
    LOW,
    NORMAL,
    Event,
    Highlight,
    loud_events,
    loudness_curve,
    word_events,
)

US = 1_000_000
AUTO = 0  # length 0 = short moments (the classic finder)
LENGTHS_S = (20, 30, 45, 60)
LONG_CLIP_S = 180  # longer clips default to Shorts-length moments
DEFAULT_LENGTH_S = 30
PEAK_AT = 0.6  # the strongest event sits at 60 % of the window: setup before, reaction after
KIND_CAP = 1.6  # one kind of evidence can add at most this much to a window
MIN_STRENGTH = {LOW: 1.0, NORMAL: 0.7, HIGH: 0.5}
SNAP_BEFORE_S, SNAP_AFTER_S = 4.0, 3.0  # how far an edge may move to a sentence boundary
QUOTE_CHARS = 90


def default_length(duration_us: int) -> int:
    return DEFAULT_LENGTH_S if duration_us > LONG_CLIP_S * US else AUTO


def default_limit(duration_us: int) -> int:
    """About one Short per three minutes of recording, 5..30."""
    return int(min(30, max(5, duration_us / US / 180)))


def strength_to_score(strength: float) -> float:
    return float(1.0 - math.exp(-strength / 1.5))


class _Index:
    """Fast "sum of scores between a and b" per kind of evidence."""

    def __init__(self, events: list[Event]) -> None:
        by_kind: dict[str, list[Event]] = defaultdict(list)
        for e in events:
            by_kind[e.reason].append(e)
        self.kinds = {}
        for kind, items in by_kind.items():
            items.sort(key=lambda e: e.t_us)
            times = [e.t_us for e in items]
            scores = np.array([min(0.95, e.score) for e in items])
            sums = np.concatenate([[0.0], np.cumsum(scores)])
            moments = np.concatenate([[0.0], np.cumsum(scores * np.array(times, dtype=np.float64))])
            self.kinds[kind] = (times, sums, moments)

    def _ranges(self, start_us: int, end_us: int):
        for kind, (times, sums, moments) in self.kinds.items():
            lo = bisect.bisect_left(times, start_us)
            hi = bisect.bisect_right(times, end_us)
            if hi > lo:
                yield kind, float(sums[hi] - sums[lo]), float(moments[hi] - moments[lo])

    def parts(self, start_us: int, end_us: int) -> dict[str, float]:
        return {kind: total for kind, total, _ in self._ranges(start_us, end_us)}

    def strength(self, start_us: int, end_us: int) -> tuple[float, float]:
        """(strength, where the evidence is centred: 0 = start .. 1 = end)."""
        parts = list(self._ranges(start_us, end_us))
        if not parts or {k for k, _, _ in parts} == {HYPE}:
            return 0.0, 0.5  # hype words alone are not a moment
        total = sum(t for _, t, _ in parts)
        centre = sum(m for _, _, m in parts) / total if total else (start_us + end_us) / 2
        span = max(1, end_us - start_us)
        return sum(min(KIND_CAP, t) for _, t, _ in parts), (centre - start_us) / span


def _candidates(events: list[Event], length_us: int, duration_us: int) -> list[int]:
    latest = max(0, duration_us - length_us)
    starts = set()
    for e in events:
        for at in (PEAK_AT, 0.25, 0.85):
            starts.add(min(latest, max(0, e.t_us - int(at * length_us))))
    return sorted(starts)


def _rank(index: _Index, start: int, length_us: int) -> tuple[float, float]:
    strength, centre = index.strength(start, start + length_us)
    # Equal strength: prefer the window with the action at PEAK_AT (setup before, reaction after).
    return round(strength, 3), abs(centre - PEAK_AT)


def pick_windows(events: list[Event], duration_us: int, length_us: int, limit: int,
                 min_strength: float) -> list[tuple[int, int, float]]:
    """Strongest non-overlapping windows: (start, end, strength).

    Greedy with lazy updates: the evidence inside a chosen window is used up,
    so the leftovers of one moment do not become a weaker copy of it."""
    if not events or duration_us <= 0:
        return []
    length_us = min(length_us, duration_us)
    remaining = sorted(events, key=lambda e: e.t_us)
    index = _Index(remaining)
    heap = []
    for start in _candidates(events, length_us, duration_us):
        strength, off = _rank(index, start, length_us)
        if strength >= min_strength:
            heap.append((-strength, off, start))
    heapq.heapify(heap)
    chosen: list[tuple[int, int, float]] = []
    while heap and len(chosen) < limit:
        neg, off, start = heapq.heappop(heap)
        end = start + length_us
        if any(start < c_end and c_start < end for c_start, c_end, _ in chosen):
            continue
        strength, new_off = _rank(index, start, length_us)
        if strength < min_strength:
            continue
        if (strength, new_off) != (-neg, off):
            heapq.heappush(heap, (-strength, new_off, start))  # stale: evidence was used up
            continue
        chosen.append((start, end, strength))
        remaining = [e for e in remaining if not start <= e.t_us <= end]
        index = _Index(remaining)
    return chosen


# ------------------------------------------------------------------ edges
def sentence_bounds(transcript) -> tuple[list[int], list[int]]:
    if transcript is None:
        return [], []
    starts = sorted(s.start_us for s in transcript.segments if s.text.strip())
    ends = sorted(s.end_us for s in transcript.segments if s.text.strip())
    return starts, ends


def _nearest(values: list[int], target: int, lo: int, hi: int) -> int | None:
    a, b = bisect.bisect_left(values, lo), bisect.bisect_right(values, hi)
    inside = values[a:b]
    return min(inside, key=lambda v: abs(v - target)) if inside else None


def _quiet_point(curve, target_us: int, lo_us: int, hi_us: int) -> int | None:
    if curve is None:
        return None
    times, db = curve
    if times.size == 0:
        return None
    a = int(np.searchsorted(times, lo_us / US))
    b = int(np.searchsorted(times, hi_us / US, side="right"))
    if b - a < 2:
        return None
    i = a + int(np.argmin(db[a:b]))
    here = db[min(db.size - 1, int(np.searchsorted(times, target_us / US)))]
    if db[i] > here - 3.0:
        return None  # nothing clearly quieter nearby: keep the edge
    return int(times[i] * US)


def snap(start_us: int, end_us: int, duration_us: int, bounds: tuple[list[int], list[int]],
         curve=None) -> tuple[int, int]:
    """Move the edges to the start/end of a sentence, else to a quiet point."""
    starts, ends = bounds
    length = end_us - start_us
    before, after = int(SNAP_BEFORE_S * US), int(SNAP_AFTER_S * US)
    s = _nearest(starts, start_us, start_us - before, start_us + after)
    if s is not None:
        s = max(0, s - 150_000)  # a breath before the first word
    else:
        s = _quiet_point(curve, start_us, start_us - after, start_us + after // 2)
    e = _nearest(ends, end_us, end_us - after, end_us + before)
    if e is not None:
        e = min(duration_us, e + 300_000)
    else:
        e = _quiet_point(curve, end_us, end_us - after // 2, end_us + after)
    s = start_us if s is None else s
    e = end_us if e is None else e
    s, e = max(0, s), min(duration_us, e)
    if e - s < 0.7 * length or e - s > 1.35 * length:
        return max(0, start_us), min(duration_us, end_us)
    return s, e


def quote(transcript, start_us: int, end_us: int, peak_us: int) -> str:
    """What is said around the strongest moment (for the list and file names)."""
    if transcript is None:
        return ""
    segments = [s for s in transcript.segments if s.text.strip() and s.end_us > start_us and s.start_us < end_us]
    if not segments:
        return ""
    near = min(segments, key=lambda s: 0 if s.contains(peak_us) else min(abs(s.start_us - peak_us),
                                                                         abs(s.end_us - peak_us)))
    i = segments.index(near)
    text = near.text.strip()
    if len(text) < 30 and i + 1 < len(segments):
        text = f"{text} {segments[i + 1].text.strip()}"
    if len(text) > QUOTE_CHARS:
        text = text[:QUOTE_CHARS].rsplit(" ", 1)[0] + "…"
    return text


# ------------------------------------------------------------------ main
def collect_events(waveform, transcript, duration_us: int, sensitivity: str = NORMAL,
                   chat=None) -> list[Event]:
    events: list[Event] = []
    if waveform is not None and len(waveform.peaks):
        events += loud_events(waveform.peaks, waveform.peaks_per_second, sensitivity)
    events += word_events(transcript)
    if chat:
        from core.highlights.chat import chat_events  # noqa: PLC0415

        events += chat_events(chat, duration_us, sensitivity)
    return [e for e in events if 0 <= e.t_us <= duration_us]


def find_moments(waveform, transcript, duration_us: int, length_s: int = DEFAULT_LENGTH_S,
                 sensitivity: str = NORMAL, limit: int | None = None, chat=None) -> list[Highlight]:
    """Shorts-length moments, best first."""
    if limit is None:
        limit = default_limit(duration_us)
    events = collect_events(waveform, transcript, duration_us, sensitivity, chat)
    length_us = int(length_s * US)
    windows = pick_windows(events, duration_us, length_us, limit, MIN_STRENGTH.get(sensitivity, 0.7))
    curve = None
    if waveform is not None and len(waveform.peaks):
        curve = loudness_curve(waveform.peaks, waveform.peaks_per_second)
    bounds = sentence_bounds(transcript)
    index = _Index(events)
    result: list[Highlight] = []
    for start, end, strength in windows:
        s, e = snap(start, end, duration_us, bounds, curve)
        inside = [ev for ev in events if s <= ev.t_us <= e] or [ev for ev in events if start <= ev.t_us <= end]
        peak = max(inside, key=lambda ev: ev.score).t_us if inside else (s + e) // 2
        parts = index.parts(s, e) or index.parts(start, end)  # all evidence, for the labels
        reasons = tuple(k for k, _ in sorted(parts.items(), key=lambda kv: -min(KIND_CAP, kv[1])))
        chat_count = 0
        if chat:
            from core.highlights.chat import count_in  # noqa: PLC0415

            chat_count = count_in(chat, s / US, e / US)
        result.append(Highlight(s, e, peak, strength_to_score(strength), reasons,
                                quote(transcript, s, e, peak), False, chat_count))
    # Edges moved by snapping must not overlap the neighbour.
    ordered = sorted(result, key=lambda h: h.start_us)
    for i in range(1, len(ordered)):
        prev, cur = ordered[i - 1], ordered[i]
        if cur.start_us < prev.end_us:
            end = min(duration_us, max(prev.end_us + US, cur.end_us))
            ordered[i] = Highlight(prev.end_us, end, min(end, max(cur.peak_us, prev.end_us)),
                                   cur.score, cur.reasons, cur.quote, cur.accepted, cur.chat_count)
    return sorted((h for h in ordered if h.end_us > h.start_us), key=lambda h: -h.score)


def merge_review(old: Iterable[Highlight], new: list[Highlight]) -> list[Highlight]:
    """Keep the ticks the user made when the search runs again."""
    from dataclasses import replace  # noqa: PLC0415

    accepted = [h for h in old if h.accepted]
    out = []
    for h in new:
        hit = any(a.start_us < h.end_us and h.start_us < a.end_us for a in accepted)
        out.append(replace(h, accepted=True) if hit else h)
    return out


__all__ = ["AUTO", "LENGTHS_S", "default_length", "default_limit", "find_moments", "merge_review",
           "pick_windows", "snap", "quote", "collect_events", "HIGH", "LOW", "NORMAL"]

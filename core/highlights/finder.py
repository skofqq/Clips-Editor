"""Find the moments of a clip worth a Short.

Two kinds of evidence, both local and fast:

* **Sound**: the loudness curve (from the cached waveform peaks) is
  compared with its own rolling median, so a moment counts when it is much
  louder than what surrounds it: shouting, a scream, a sudden roar.
* **Words** in the transcript: laughter ("ахаха", "lol", "(смех)"),
  stretched words ("дааааа", "noooo"), exclamations and caps, and hype
  words ("вау", "жесть", "клатч", "omg").

Events closer than a few seconds form one highlight; its range starts a
little before (the setup) and ends a little after (the reaction). Nothing is
cut: the user picks a highlight and decides what to do with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

LOW, NORMAL, HIGH = "low", "normal", "high"
SENSITIVITY_DB = {LOW: 12.0, NORMAL: 9.0, HIGH: 6.0}  # dB above the surroundings
MIN_LEVEL_DB = -38.0  # ignore spikes in near silence
STEP_S = 0.1
WINDOW_S = 0.3
BASELINE_S = 8.0
MIN_GAP_S = 3.0  # loudness peaks closer than this are one event
GROUP_S = 4.0  # events closer than this are one highlight
LEAD_S = 5.0  # range starts this long before the first event (the setup)
TAIL_S = 3.0  # and ends this long after the last one (the reaction)

LOUD, LAUGH, SHOUT, HYPE = "loud", "laughter", "shouting", "hype"
REASON_LABELS = {LOUD: "Loud", LAUGH: "Laughter", SHOUT: "Shouting", HYPE: "Hype words"}

_LAUGH = re.compile(
    r"(?:\b(?:a?ха){2,}|\b(?:хе){2,}|\b(?:хи){2,}|\bах(?:ах)+|\b(?:a?ha){2,}|\b(?:he){2,}|\bah(?:ah)+|"
    r"\blo+l\b|\blmao|\brofl|\bкек|\bору\b|\bорууу|\bржу|\bржака|смех|смеёт|смеет|laugh|chuckl)",
    re.IGNORECASE,
)
_STRETCH = re.compile(r"([^\W\d_])\1{3,}", re.IGNORECASE)  # дааааа, noooo
_HYPE = re.compile(
    r"\b(?:вау|воу|ого+|офиге|офигеть|охрене|жесть|жееесть|имба|клатч|ез+|изи|гг|го+у?|пого|погнали|"
    r"красав|красава|невероят|нифига|ничего себе|боже|сука|бля|пиздец|ебать|wow|omg|oh my god|insane|clutch|"
    r"let'?s go|ez+|gg|pog|no way|what the|holy)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Event:
    t_us: int
    score: float  # 0..1
    reason: str


@dataclass(frozen=True)
class Highlight:
    start_us: int
    end_us: int
    peak_us: int
    score: float  # 0..1
    reasons: tuple[str, ...]

    @property
    def label(self) -> str:
        return ", ".join(REASON_LABELS.get(r, r) for r in self.reasons)

    def to_dict(self) -> dict[str, Any]:
        return {"start_us": self.start_us, "end_us": self.end_us, "peak_us": self.peak_us,
                "score": round(self.score, 3), "reasons": list(self.reasons)}

    @classmethod
    def from_dict(cls, data: Any) -> "Highlight":
        if not isinstance(data, dict):
            raise ValueError("Highlight must be an object.")
        start, end = int(data["start_us"]), int(data["end_us"])
        if end <= start:
            raise ValueError("Highlight range is empty.")
        return cls(start, end, int(data.get("peak_us", start)), float(data.get("score", 0.0)),
                   tuple(str(r) for r in data.get("reasons") or ()))


# ------------------------------------------------------------------ sound
def loudness_curve(peaks: np.ndarray, peaks_per_second: int) -> tuple[np.ndarray, np.ndarray]:
    """(times in s, level in dBFS) every STEP_S from 0..255 peak values."""
    x = np.asarray(peaks, dtype=np.float32) / 255.0
    if x.size == 0:
        return np.zeros(0), np.zeros(0)
    step = max(1, int(round(STEP_S * peaks_per_second)))
    win = max(step, int(round(WINDOW_S * peaks_per_second)))
    starts = np.arange(0, max(1, x.size - win + 1), step)
    power = np.cumsum(np.concatenate([[0.0], x.astype(np.float64) ** 2]))
    rms = np.sqrt((power[np.minimum(starts + win, x.size)] - power[starts]) / win)
    db = 20 * np.log10(np.maximum(rms, 1e-5))
    times = (starts + win / 2) / peaks_per_second
    return times, db


def _rolling_median(values: np.ndarray, size: int) -> np.ndarray:
    if values.size == 0:
        return values
    size = max(1, min(size, values.size))
    half = size // 2
    padded = np.pad(values, (half, size - 1 - half), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, size)
    return np.median(windows, axis=1)


def loud_events(peaks: np.ndarray, peaks_per_second: int, sensitivity: str = NORMAL) -> list[Event]:
    times, db = loudness_curve(peaks, peaks_per_second)
    if times.size < 3:
        return []
    baseline = _rolling_median(db, int(BASELINE_S / STEP_S))
    excess = db - baseline
    threshold = SENSITIVITY_DB.get(sensitivity, SENSITIVITY_DB[NORMAL])
    candidates = np.where((excess >= threshold) & (db >= MIN_LEVEL_DB))[0]
    events: list[Event] = []
    min_gap = int(MIN_GAP_S / STEP_S)
    for i in sorted(candidates, key=lambda k: -excess[k]):  # strongest first, keep them apart
        if any(abs(int(i) - int(round(e.t_us / 1e6 / STEP_S))) < min_gap for e in events):
            continue
        score = float(np.clip(0.45 + (excess[i] - threshold) / 15.0, 0.0, 1.0))
        events.append(Event(int(times[i] * 1_000_000), score, LOUD))
    return sorted(events, key=lambda e: e.t_us)


# ------------------------------------------------------------------ words
def word_reasons(text: str) -> list[tuple[str, float]]:
    """Cues in one word (or a bracketed annotation like "[смех]")."""
    found = []
    if _LAUGH.search(text):
        found.append((LAUGH, 0.7))
    stripped = re.sub(r"[^\w]", "", text)
    if _STRETCH.search(stripped) and not _LAUGH.search(text):
        found.append((SHOUT, 0.55))
    elif "!" in text and len(stripped) >= 2:
        found.append((SHOUT, 0.35))
    if len(stripped) >= 3 and stripped.isupper() and any(c.isalpha() for c in stripped):
        found.append((SHOUT, 0.4))
    if _HYPE.search(text):
        found.append((HYPE, 0.4))
    return found


def word_events(transcript) -> list[Event]:
    events: list[Event] = []
    if transcript is None:
        return events
    for segment in transcript.segments:
        words = segment.words or ()
        if words:
            items = [(w.start_us, w.text) for w in words]
        else:
            items = [(segment.start_us, segment.text)]
        # Two-word phrases ("oh my god", "ничего себе", "let's go").
        for i, (t, text) in enumerate(items):
            pair = text + " " + items[i + 1][1] if i + 1 < len(items) else text
            reasons = word_reasons(text) or [r for r in word_reasons(pair) if r[0] == HYPE]
            for reason, score in reasons:
                events.append(Event(int(t), score, reason))
    return events


# ------------------------------------------------------------------ combine
def group(events: Iterable[Event], duration_us: int, limit: int = 12) -> list[Highlight]:
    events = sorted(events, key=lambda e: e.t_us)
    clusters: list[list[Event]] = []
    for e in events:
        if clusters and e.t_us - clusters[-1][-1].t_us <= GROUP_S * 1_000_000:
            clusters[-1].append(e)
        else:
            clusters.append([e])
    result = []
    for cluster in clusters:
        miss = 1.0
        for e in cluster:
            miss *= 1.0 - min(0.95, e.score)
        score = 1.0 - miss
        if score < 0.5 and all(e.reason == HYPE for e in cluster):
            continue  # a single hype word alone is not a moment
        peak = max(cluster, key=lambda e: e.score)
        start = max(0, cluster[0].t_us - int(LEAD_S * 1_000_000))
        end = min(duration_us, cluster[-1].t_us + int(TAIL_S * 1_000_000))
        if end <= start:
            continue
        reasons = tuple(dict.fromkeys(e.reason for e in sorted(cluster, key=lambda e: -e.score)))
        result.append(Highlight(start, end, peak.t_us, score, reasons))
    # Keep the best ones, then present them in clip order; merge overlaps.
    best = sorted(result, key=lambda h: -h.score)[:limit]
    best.sort(key=lambda h: h.start_us)
    merged: list[Highlight] = []
    for h in best:
        if merged and h.start_us < merged[-1].end_us:
            m = merged[-1]
            peak = m if m.score >= h.score else h
            merged[-1] = Highlight(m.start_us, max(m.end_us, h.end_us), peak.peak_us, max(m.score, h.score),
                                   tuple(dict.fromkeys(m.reasons + h.reasons)))
        else:
            merged.append(h)
    return merged


def find_highlights(waveform, transcript, duration_us: int, sensitivity: str = NORMAL,
                    limit: int = 12) -> list[Highlight]:
    events: list[Event] = []
    if waveform is not None and len(waveform.peaks):
        events += loud_events(waveform.peaks, waveform.peaks_per_second, sensitivity)
    events += word_events(transcript)
    return group(events, duration_us, limit)

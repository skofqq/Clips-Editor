"""Censor suggestions: detection, review and applying to the timeline.

The flow is deliberately two-step:

1. :func:`detect` scans a transcript and returns a :class:`CensorState`
   with *pending* suggestions. Nothing is muted.
2. The user accepts / rejects / adjusts each suggestion. Only
   :meth:`CensorState.apply` touches the timeline, and it only uses
   suggestions whose status is ``accepted``.

All times are integer microseconds in source time, like the timeline.
Mute regions added by the censor are remembered by id, so applying again
replaces them instead of stacking duplicates; regions the user drew by
hand are never touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from core.censor.dictionary import CUSTOM, MILD, STRONG, Dictionary
from core.censor.normalize import language_of
from core.timeline.model import BEEP, MUTE_MODES, SILENCE, Timeline
from core.transcription.model import Transcript

PENDING = "pending"
ACCEPTED = "accepted"
REJECTED = "rejected"
STATUSES = (PENDING, ACCEPTED, REJECTED)

DEFAULT_PAD_BEFORE_US = 150_000
DEFAULT_PAD_AFTER_US = 250_000
MAX_PAD_US = 2_000_000
MIN_PAD_US = -1_000_000  # negative padding mutes only the inside of the word
MIN_RANGE_US = 20_000


@dataclass(frozen=True)
class CensorSettings:
    pad_before_us: int = DEFAULT_PAD_BEFORE_US
    pad_after_us: int = DEFAULT_PAD_AFTER_US
    mode: str = SILENCE  # silence | beep
    categories: tuple[str, ...] = (STRONG, CUSTOM)

    def cleaned(self) -> "CensorSettings":
        cats = tuple(c for c in (STRONG, MILD, CUSTOM) if c in self.categories)
        return CensorSettings(
            pad_before_us=max(MIN_PAD_US, min(MAX_PAD_US, int(self.pad_before_us))),
            pad_after_us=max(MIN_PAD_US, min(MAX_PAD_US, int(self.pad_after_us))),
            mode=self.mode if self.mode in MUTE_MODES else SILENCE,
            categories=cats,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pad_before_us": self.pad_before_us,
            "pad_after_us": self.pad_after_us,
            "mode": self.mode,
            "categories": list(self.categories),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "CensorSettings":
        if not isinstance(data, dict):
            return cls()
        default = cls()
        cats = data.get("categories")
        return cls(
            pad_before_us=int(data.get("pad_before_us", default.pad_before_us)),
            pad_after_us=int(data.get("pad_after_us", default.pad_after_us)),
            mode=str(data.get("mode", default.mode)),
            categories=tuple(str(c) for c in cats) if isinstance(cats, list) else default.categories,
        ).cleaned()


@dataclass(frozen=True)
class Suggestion:
    id: int
    segment_id: int
    word_index: int
    word_start_us: int  # the recognised word itself
    word_end_us: int
    start_us: int  # range to mute (padded, user-adjustable)
    end_us: int
    text: str  # word as recognised
    entry_key: str  # dictionary entry that matched
    category: str
    language: str
    exact: bool  # False when the word only has approximate (segment) timing
    status: str = PENDING
    adjusted: bool = False  # True once the user moved start/end by hand

    @property
    def key(self) -> tuple[int, int, int]:
        """Identity across re-detection: the same word at the same place."""
        return (self.segment_id, self.word_index, self.word_start_us)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "segment_id": self.segment_id,
            "word_index": self.word_index,
            "word_start_us": self.word_start_us,
            "word_end_us": self.word_end_us,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "text": self.text,
            "entry_key": self.entry_key,
            "category": self.category,
            "language": self.language,
            "exact": self.exact,
            "status": self.status,
            "adjusted": self.adjusted,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Suggestion":
        status = str(data.get("status", PENDING))
        start, end = int(data["start_us"]), int(data["end_us"])
        if end <= start:
            raise ValueError("Suggestion range is empty.")
        return cls(
            id=int(data["id"]),
            segment_id=int(data.get("segment_id", 0)),
            word_index=int(data.get("word_index", 0)),
            word_start_us=int(data.get("word_start_us", start)),
            word_end_us=int(data.get("word_end_us", end)),
            start_us=start,
            end_us=end,
            text=str(data.get("text", "")),
            entry_key=str(data.get("entry_key", "")),
            category=str(data.get("category", STRONG)),
            language=str(data.get("language", "")),
            exact=bool(data.get("exact", True)),
            status=status if status in STATUSES else PENDING,
            adjusted=bool(data.get("adjusted", False)),
        )


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or touching ``(start, end)`` ranges."""
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


@dataclass(frozen=True)
class CensorState:
    suggestions: tuple[Suggestion, ...] = ()
    settings: CensorSettings = field(default_factory=CensorSettings)
    applied_mute_ids: tuple[int, ...] = ()  # timeline mutes owned by the censor
    next_id: int = 1

    # ------------------------------------------------------------ queries
    def get(self, suggestion_id: int) -> Suggestion | None:
        return next((s for s in self.suggestions if s.id == suggestion_id), None)

    def count(self, status: str) -> int:
        return sum(1 for s in self.suggestions if s.status == status)

    def accepted_ranges(self) -> list[tuple[int, int]]:
        return merge_ranges([(s.start_us, s.end_us) for s in self.suggestions if s.status == ACCEPTED])

    def is_applied(self, timeline: Timeline | None) -> bool:
        """True when the timeline's censor mutes match the accepted ranges."""
        if timeline is None:
            return not self.accepted_ranges()
        owned = sorted(
            (m.start_us, m.end_us) for m in timeline.mutes if m.id in self.applied_mute_ids
        )
        wanted = [
            (max(0, a), min(timeline.duration_us, b)) for a, b in self.accepted_ranges()
        ]
        return owned == wanted and all(
            m.mode == self.settings.mode for m in timeline.mutes if m.id in self.applied_mute_ids
        )

    # ------------------------------------------------------------ editing
    def _replace(self, updated: Suggestion) -> "CensorState":
        items = tuple(updated if s.id == updated.id else s for s in self.suggestions)
        return replace(self, suggestions=items)

    def with_status(self, ids, status: str) -> "CensorState":
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        ids = set(ids)
        items = tuple(replace(s, status=status) if s.id in ids else s for s in self.suggestions)
        return self if items == self.suggestions else replace(self, suggestions=items)

    def with_all_status(self, status: str, only: str | None = None) -> "CensorState":
        ids = [s.id for s in self.suggestions if only is None or s.status == only]
        return self.with_status(ids, status)

    def with_range(
        self, suggestion_id: int, start_us: int | None = None, end_us: int | None = None, duration_us: int = 0
    ) -> "CensorState":
        s = self.get(suggestion_id)
        if s is None:
            return self
        start = s.start_us if start_us is None else int(start_us)
        end = s.end_us if end_us is None else int(end_us)
        start = max(0, start)
        if duration_us > 0:
            end = min(duration_us, end)
        if start_us is not None and end - start < MIN_RANGE_US:
            start = end - MIN_RANGE_US
        elif end - start < MIN_RANGE_US:
            end = start + MIN_RANGE_US
        if (start, end) == (s.start_us, s.end_us):
            return self
        return self._replace(replace(s, start_us=start, end_us=end, adjusted=True))

    def with_settings(self, settings: CensorSettings, duration_us: int = 0) -> "CensorState":
        """Change settings; padding is re-applied to suggestions not adjusted by hand."""
        settings = settings.cleaned()
        if settings == self.settings:
            return self
        items = tuple(
            s if s.adjusted else replace(s, **_padded(s.word_start_us, s.word_end_us, settings, duration_us))
            for s in self.suggestions
        )
        return replace(self, settings=settings, suggestions=items)

    # ------------------------------------------------------------- apply
    def apply(self, timeline: Timeline) -> tuple[Timeline, "CensorState"]:
        """Replace the censor's mutes in ``timeline`` with the accepted ranges.

        Pending and rejected suggestions are ignored: nothing is muted
        without the user's explicit acceptance.
        """
        result = timeline
        for mute_id in self.applied_mute_ids:
            result = result.delete_mute(mute_id)
        owned: list[int] = []
        for start, end in self.accepted_ranges():
            before = result.next_id
            updated = result.add_mute(start, end, self.settings.mode)
            if updated is not result:
                owned.append(before)
                result = updated
        return result, replace(self, applied_mute_ids=tuple(owned))

    def remove_applied(self, timeline: Timeline) -> tuple[Timeline, "CensorState"]:
        result = timeline
        for mute_id in self.applied_mute_ids:
            result = result.delete_mute(mute_id)
        return result, replace(self, applied_mute_ids=())

    # ------------------------------------------------------- serialisation
    def to_dict(self) -> dict[str, Any]:
        return {
            "settings": self.settings.to_dict(),
            "suggestions": [s.to_dict() for s in self.suggestions],
            "applied_mute_ids": list(self.applied_mute_ids),
            "next_id": self.next_id,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "CensorState":
        if not isinstance(data, dict):
            raise ValueError("Censor state must be an object.")
        suggestions = []
        for item in data.get("suggestions") or []:
            if isinstance(item, dict):
                suggestions.append(Suggestion.from_dict(item))
        suggestions.sort(key=lambda s: (s.start_us, s.id))
        next_id = max(max((s.id for s in suggestions), default=0) + 1, int(data.get("next_id") or 1))
        return cls(
            suggestions=tuple(suggestions),
            settings=CensorSettings.from_dict(data.get("settings")),
            applied_mute_ids=tuple(int(i) for i in data.get("applied_mute_ids") or []),
            next_id=next_id,
        )


# Negative padding leaves the edges of a word audible ("б…"). It may never
# uncover more than this share of the word on each side, and words shorter
# than SHORT_WORD_US are always muted whole: their edges ARE the word.
MAX_UNCOVER_SHARE = 0.25
SHORT_WORD_US = 200_000


def _padded(word_start: int, word_end: int, settings: CensorSettings, duration_us: int) -> dict[str, int]:
    length = max(0, word_end - word_start)
    before, after = settings.pad_before_us, settings.pad_after_us
    if before < 0 or after < 0:
        limit = 0 if length < SHORT_WORD_US else int(length * MAX_UNCOVER_SHARE)
        if before < 0:
            before = -min(-before, limit)
        if after < 0:
            after = -min(-after, limit)
    start = max(0, word_start - before)
    end = word_end + after
    if duration_us > 0:
        end = min(duration_us, end)
    if end - start < MIN_RANGE_US:
        end = start + MIN_RANGE_US
    return {"start_us": start, "end_us": end}


def detect(
    transcript: Transcript | None,
    dictionary: Dictionary,
    settings: CensorSettings | None = None,
    previous: CensorState | None = None,
    duration_us: int = 0,
) -> CensorState:
    """Find words to censor and return them as *pending* suggestions.

    If ``previous`` is given, review decisions and hand-adjusted ranges of
    suggestions for the same word are kept, and so are the ids of mutes
    already applied, so the next Apply replaces them.
    """
    settings = (settings or (previous.settings if previous else CensorSettings())).cleaned()
    duration_us = duration_us or (transcript.duration_us if transcript else 0)
    old = {s.key: s for s in previous.suggestions} if previous else {}
    next_id = previous.next_id if previous else 1
    found: list[Suggestion] = []
    if transcript is not None and settings.categories:
        for segment in transcript.segments:
            for index, word in enumerate(segment.words):
                match = dictionary.match(word.text, settings.categories)
                if match is None:
                    continue
                key = (segment.id, index, word.start_us)
                prior = old.get(key)
                language = match.language if match.language in ("ru", "en") else language_of(word.text)
                if prior is not None:
                    item = replace(
                        prior,
                        word_end_us=word.end_us,
                        text=word.text,
                        entry_key=match.entry_key,
                        category=match.category,
                        language=language,
                        exact=word.exact,
                    )
                    if not prior.adjusted:
                        item = replace(item, **_padded(word.start_us, word.end_us, settings, duration_us))
                else:
                    item = Suggestion(
                        id=next_id,
                        segment_id=segment.id,
                        word_index=index,
                        word_start_us=word.start_us,
                        word_end_us=word.end_us,
                        text=word.text,
                        entry_key=match.entry_key,
                        category=match.category,
                        language=language,
                        exact=word.exact,
                        **_padded(word.start_us, word.end_us, settings, duration_us),
                    )
                    next_id += 1
                found.append(item)
    found.sort(key=lambda s: (s.start_us, s.id))
    applied = previous.applied_mute_ids if previous else ()
    return CensorState(tuple(found), settings, applied, next_id)


__all__ = [
    "ACCEPTED",
    "BEEP",
    "CensorSettings",
    "CensorState",
    "PENDING",
    "REJECTED",
    "SILENCE",
    "STATUSES",
    "Suggestion",
    "detect",
    "merge_ranges",
]

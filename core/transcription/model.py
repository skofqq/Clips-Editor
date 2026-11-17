"""Transcript data model.

Times are integer microseconds in **source** time, like the timeline, so
words map directly onto mute regions (Phase 5).

Every word carries ``timing``:

* ``"word"`` – the recogniser produced a timestamp for this word.
* ``"segment"`` – no reliable word timing exists (the user rewrote the
  segment text, or the model gave none). The word only inherits its
  segment's span; callers must treat it as approximate.

The model is immutable; edits return a new :class:`Transcript`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

WORD_TIMING = "word"
SEGMENT_TIMING = "segment"
LANGUAGES = {"ru": "Russian", "en": "English"}

_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Word:
    start_us: int
    end_us: int
    text: str
    probability: float = 1.0
    timing: str = WORD_TIMING

    @property
    def exact(self) -> bool:
        return self.timing == WORD_TIMING


@dataclass(frozen=True)
class Segment:
    id: int
    start_us: int
    end_us: int
    text: str
    words: tuple[Word, ...] = ()
    edited: bool = False

    @property
    def has_word_timing(self) -> bool:
        return bool(self.words) and all(w.exact for w in self.words)

    def contains(self, us: int) -> bool:
        return self.start_us <= us < self.end_us


def _clean(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def _tokens(text: str) -> list[str]:
    return _clean(text).split(" ") if _clean(text) else []


def rewrite_words(segment: Segment, text: str) -> tuple[Word, ...]:
    """Words for ``segment`` after its text became ``text``.

    If the number of words is unchanged, each word keeps its own timing
    (a spelling fix keeps exact timestamps). Otherwise the alignment is
    lost and every word gets the segment span with ``segment`` timing.
    """
    tokens = _tokens(text)
    if segment.words and len(tokens) == len(segment.words):
        return tuple(replace(w, text=t) for w, t in zip(segment.words, tokens))
    return tuple(
        Word(segment.start_us, segment.end_us, t, 0.0, SEGMENT_TIMING) for t in tokens
    )


@dataclass(frozen=True)
class Transcript:
    language: str  # ISO code, e.g. "ru"
    language_probability: float
    model: str
    segments: tuple[Segment, ...]
    duration_us: int = 0

    # ------------------------------------------------------------ queries
    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments if s.text)

    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    def segment(self, segment_id: int) -> Segment | None:
        return next((s for s in self.segments if s.id == segment_id), None)

    def segment_at(self, us: int) -> Segment | None:
        return next((s for s in self.segments if s.contains(us)), None)

    def word_at(self, us: int) -> Word | None:
        segment = self.segment_at(us)
        if segment is None:
            return None
        return next((w for w in segment.words if w.exact and w.start_us <= us < w.end_us), None)

    @property
    def exact_word_ratio(self) -> float:
        words = self.words()
        return sum(w.exact for w in words) / len(words) if words else 0.0

    # -------------------------------------------------------------- edits
    def with_segment_text(self, segment_id: int, text: str) -> "Transcript":
        segment = self.segment(segment_id)
        text = _clean(text)
        if segment is None or text == segment.text:
            return self
        updated = replace(segment, text=text, words=rewrite_words(segment, text), edited=True)
        return self._replace_segment(updated)

    def with_word_text(self, segment_id: int, index: int, text: str) -> "Transcript":
        segment = self.segment(segment_id)
        text = _clean(text)
        if segment is None or not 0 <= index < len(segment.words) or not text or " " in text:
            return self
        if segment.words[index].text == text:
            return self
        words = list(segment.words)
        words[index] = replace(words[index], text=text)
        updated = replace(segment, words=tuple(words), text=" ".join(w.text for w in words), edited=True)
        return self._replace_segment(updated)

    def with_range_replaced(self, start_us: int, end_us: int, new_segments) -> "Transcript":
        """Replace every segment overlapping ``start_us``–``end_us`` by ``new_segments``.

        Segment ids are renumbered in time order.
        """
        kept = [s for s in self.segments if s.end_us <= start_us or s.start_us >= end_us]
        merged = sorted(kept + list(new_segments), key=lambda s: (s.start_us, s.end_us))
        segments = tuple(replace(s, id=i) for i, s in enumerate(merged, start=1))
        return replace(self, segments=segments)

    def covering_range(self, start_us: int, end_us: int) -> tuple[int, int]:
        """``start_us``–``end_us`` widened to whole segments it touches."""
        touched = [s for s in self.segments if s.end_us > start_us and s.start_us < end_us]
        if not touched:
            return start_us, end_us
        return min(start_us, touched[0].start_us), max(end_us, touched[-1].end_us)

    def _replace_segment(self, updated: Segment) -> "Transcript":
        segments = tuple(updated if s.id == updated.id else s for s in self.segments)
        return replace(self, segments=segments)

    # ------------------------------------------------------ serialisation
    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "language_probability": round(self.language_probability, 4),
            "model": self.model,
            "duration_us": self.duration_us,
            "segments": [
                {
                    "id": s.id,
                    "start_us": s.start_us,
                    "end_us": s.end_us,
                    "text": s.text,
                    "edited": s.edited,
                    "words": [
                        [w.start_us, w.end_us, w.text, round(w.probability, 4), w.timing] for w in s.words
                    ],
                }
                for s in self.segments
            ],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Transcript":
        if not isinstance(data, dict):
            raise ValueError("Transcript must be an object.")
        segments: list[Segment] = []
        seen: set[int] = set()
        for item in data.get("segments") or []:
            seg_id, start, end = int(item["id"]), int(item["start_us"]), int(item["end_us"])
            if seg_id in seen or start < 0 or end < start:
                raise ValueError("Invalid transcript segment.")
            seen.add(seg_id)
            words = []
            for raw in item.get("words") or []:
                w_start, w_end, w_text, prob, timing = raw
                if timing not in (WORD_TIMING, SEGMENT_TIMING) or int(w_end) < int(w_start):
                    raise ValueError("Invalid transcript word.")
                words.append(Word(int(w_start), int(w_end), str(w_text), float(prob), timing))
            segments.append(
                Segment(seg_id, start, end, str(item.get("text", "")), tuple(words), bool(item.get("edited")))
            )
        segments.sort(key=lambda s: (s.start_us, s.id))
        return cls(
            language=str(data.get("language") or ""),
            language_probability=float(data.get("language_probability") or 0.0),
            model=str(data.get("model") or ""),
            segments=tuple(segments),
            duration_us=int(data.get("duration_us") or 0),
        )

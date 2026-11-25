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

import difflib
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
    speaker: int = -1  # 0, 1, … after "Find speakers"; -1 = unknown

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


def _key(text: str) -> str:
    """A word compared loosely: case, ё/е and punctuation do not matter."""
    return "".join(ch for ch in text.casefold().replace("ё", "е") if ch.isalnum())


def _weight(text: str) -> int:
    return max(1, sum(ch.isalnum() for ch in text))


def spread(start_us: int, end_us: int, tokens: list[str], like: Word) -> list[Word]:
    """``tokens`` sharing ``start_us``–``end_us`` by their number of letters.

    Speaker, confidence and timing kind come from ``like``. Words without exact
    timing all keep the whole span, like any approximate word.
    """
    if not like.exact:
        return [replace(like, start_us=start_us, end_us=end_us, text=t) for t in tokens]
    total = sum(_weight(t) for t in tokens)
    span = max(0, end_us - start_us)
    words, done, at = [], 0, start_us
    for token in tokens:
        done += _weight(token)
        end = start_us + span * done // total
        words.append(replace(like, start_us=at, end_us=end, text=token))
        at = end
    return words


# An inserted word gets the pause next to it when the pause is at least this long
# per word; otherwise it shares the time of the word next to it.
_MIN_GAP_PER_WORD_US = 120_000


def rewrite_words(segment: Segment, text: str) -> tuple[Word, ...]:
    """Words for ``segment`` after its text became ``text``.

    Words that stay the same keep their own timing. A changed stretch takes the time
    of the words it replaces (split by letters), an inserted word takes the pause
    next to it or shares the neighbouring word's time, and deleted words go away.
    Only a segment without word timing at all gives every word the segment span.
    """
    tokens = _tokens(text)
    old = list(segment.words)
    if not old:
        return tuple(
            Word(segment.start_us, segment.end_us, t, 0.0, SEGMENT_TIMING, -1) for t in tokens
        )
    matcher = difflib.SequenceMatcher(None, [_key(w.text) for w in old], [_key(t) for t in tokens],
                                      autojunk=False)
    # First pass: kept and replaced words get times, inserted ones are plain strings.
    items: list[Word | str] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        span, new = old[i1:i2], tokens[j1:j2]
        if op == "equal" or (op == "replace" and len(span) == len(new)):
            items.extend(replace(w, text=t) for w, t in zip(span, new))
        elif op == "replace":
            like = span[0] if all(w.exact for w in span) else replace(span[0], timing=SEGMENT_TIMING)
            items.extend(spread(span[0].start_us, span[-1].end_us, new, like))
        elif op == "insert":
            items.extend(new)
    # Second pass: each run of inserted words goes into the pause beside it, or
    # shares the time of the word before it (else after it).
    out: list[Word] = []
    i = 0
    while i < len(items):
        if isinstance(items[i], Word):
            out.append(items[i])
            i += 1
            continue
        j = i
        while j < len(items) and isinstance(items[j], str):
            j += 1
        inserted = [str(t) for t in items[i:j]]
        before = out[-1] if out else None
        after = items[j] if j < len(items) else None
        gap_start = before.end_us if before else segment.start_us
        gap_end = after.start_us if after else segment.end_us
        if gap_end - gap_start >= _MIN_GAP_PER_WORD_US * len(inserted):
            out.extend(spread(gap_start, gap_end, inserted, before or after))
            i = j
        elif before is not None:
            out.pop()
            out.extend(spread(before.start_us, before.end_us, [before.text] + inserted, before))
            i = j
        else:  # inserted at the very start with no pause: share the first word
            assert isinstance(after, Word)
            out.extend(spread(after.start_us, after.end_us, inserted + [after.text], after))
            i = j + 1
    return tuple(out)


@dataclass(frozen=True)
class Transcript:
    language: str  # ISO code, e.g. "ru"
    language_probability: float
    model: str
    segments: tuple[Segment, ...]
    duration_us: int = 0
    # Double check: what a second model heard (the censor also looks at these words).
    check_model: str = ""
    check_words: tuple[Word, ...] = ()

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
    def speaker_count(self) -> int:
        return len({w.speaker for w in self.words() if w.speaker >= 0})

    def with_speakers(self, speakers: list[int]) -> "Transcript":
        """Set every word's speaker, in the order of :meth:`words`."""
        it = iter(speakers)
        segments = tuple(
            replace(s, words=tuple(replace(w, speaker=int(next(it, -1))) for w in s.words)) for s in self.segments
        )
        return replace(self, segments=segments)

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
        """Change one word. Several words split its time by letters; empty deletes it."""
        segment = self.segment(segment_id)
        if segment is None or not 0 <= index < len(segment.words):
            return self
        tokens = _tokens(text)
        word = segment.words[index]
        if tokens == [word.text] or (not tokens and len(segment.words) == 1):
            return self
        words = list(segment.words)
        words[index:index + 1] = spread(word.start_us, word.end_us, tokens, word)
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
                        [w.start_us, w.end_us, w.text, round(w.probability, 4), w.timing]
                        + ([w.speaker] if w.speaker >= 0 else [])
                        for w in s.words
                    ],
                }
                for s in self.segments
            ],
            **({"check": {"model": self.check_model,
                          "words": [[w.start_us, w.end_us, w.text, round(w.probability, 4), w.timing]
                                    for w in self.check_words]}}
               if self.check_model else {}),
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
                w_start, w_end, w_text, prob, timing = raw[:5]
                speaker = int(raw[5]) if len(raw) > 5 and isinstance(raw[5], int) else -1
                if timing not in (WORD_TIMING, SEGMENT_TIMING) or int(w_end) < int(w_start):
                    raise ValueError("Invalid transcript word.")
                words.append(Word(int(w_start), int(w_end), str(w_text), float(prob), timing, max(-1, speaker)))
            segments.append(
                Segment(seg_id, start, end, str(item.get("text", "")), tuple(words), bool(item.get("edited")))
            )
        segments.sort(key=lambda s: (s.start_us, s.id))
        check = data.get("check") if isinstance(data.get("check"), dict) else {}
        check_words = []
        for raw in check.get("words") or []:
            try:
                w_start, w_end, w_text, prob, timing = raw[:5]
                if timing in (WORD_TIMING, SEGMENT_TIMING) and int(w_end) >= int(w_start) >= 0:
                    check_words.append(Word(int(w_start), int(w_end), str(w_text), float(prob), timing))
            except (TypeError, ValueError):
                continue
        return cls(
            language=str(data.get("language") or ""),
            language_probability=float(data.get("language_probability") or 0.0),
            model=str(data.get("model") or ""),
            segments=tuple(segments),
            duration_us=int(data.get("duration_us") or 0),
            check_model=str(check.get("model") or "") if check_words else "",
            check_words=tuple(check_words),
        )

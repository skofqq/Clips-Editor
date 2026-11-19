"""Subtitles for the Short: style, captions built from the transcript, edits.

Captions are built from the transcript's timed words and the timeline:
only words inside kept segments are shown, and their times are converted
to **output time** (the time of the rendered video). A caption shows a few
words at once (Shorts style); the word being spoken can be highlighted.
Words that fall inside a mute region can be masked ("б***").

The user can force a caption to start at a word (``breaks``) or glue a
caption to the previous one (``joins``); both are keyed by the word's
source start time so they survive re-grouping.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields, replace

from core.timeline.model import Timeline
from core.transcription.model import Transcript

MASK_NONE, MASK_FIRST, MASK_ALL = "none", "first", "all"
POSITION_DIVIDER = -1.0  # sentinel: centre on the webcam/gameplay border


@dataclass(frozen=True)
class SubtitleStyle:
    enabled: bool = False
    font: str = "Segoe UI"
    size: int = 72  # px on the 1080×1920 canvas
    bold: bool = True
    uppercase: bool = True
    color: str = "#ffffff"
    highlight: str = "#ffd400"  # current word; same as color = no karaoke
    outline: str = "#000000"
    outline_px: int = 8
    shadow: bool = True
    box: bool = False  # rounded box behind the text instead of an outline
    box_color: str = "#000000b0"
    position: float = POSITION_DIVIDER  # vertical centre as a fraction of the canvas height
    max_words: int = 3
    max_chars: int = 18
    max_lines: int = 2
    gap_split_ms: int = 600  # a pause this long starts a new caption
    min_duration_ms: int = 400
    mask: str = MASK_FIRST
    karaoke: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "SubtitleStyle":
        base = cls()
        if not isinstance(data, dict):
            return base
        values = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            default = getattr(base, f.name)
            if isinstance(default, bool):
                if isinstance(value, bool):
                    values[f.name] = value
            elif isinstance(default, int):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values[f.name] = int(value)
            elif isinstance(default, float):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values[f.name] = float(value)
            elif isinstance(value, str):
                values[f.name] = value
        return replace(base, **values).cleaned()

    def cleaned(self) -> "SubtitleStyle":
        position = self.position if self.position == POSITION_DIVIDER else min(0.95, max(0.05, self.position))
        return replace(
            self,
            size=min(200, max(16, self.size)),
            outline_px=min(30, max(0, self.outline_px)),
            max_words=min(12, max(1, self.max_words)),
            max_chars=min(80, max(4, self.max_chars)),
            max_lines=min(4, max(1, self.max_lines)),
            gap_split_ms=min(5000, max(0, self.gap_split_ms)),
            min_duration_ms=min(3000, max(0, self.min_duration_ms)),
            mask=self.mask if self.mask in (MASK_NONE, MASK_FIRST, MASK_ALL) else MASK_FIRST,
            position=position,
        )


PRESETS: dict[str, SubtitleStyle] = {
    "Shorts bold": SubtitleStyle(enabled=True),
    "Karaoke green": SubtitleStyle(enabled=True, highlight="#4dff6a", max_words=2),
    "One word": SubtitleStyle(enabled=True, max_words=1, size=96, max_chars=14),
    "Clean": SubtitleStyle(enabled=True, uppercase=False, bold=True, size=60, highlight="#ffffff",
                           karaoke=False, max_words=6, max_chars=32, outline_px=5),
    "Boxed": SubtitleStyle(enabled=True, box=True, outline_px=0, uppercase=False, size=60,
                           max_words=5, max_chars=28, karaoke=False, highlight="#ffffff"),
}


@dataclass(frozen=True)
class SubtitleEdits:
    breaks: frozenset[int] = frozenset()  # word source start → caption starts here
    joins: frozenset[int] = frozenset()  # word source start → never start a caption here

    @property
    def empty(self) -> bool:
        return not self.breaks and not self.joins

    def split_at(self, word_us: int) -> "SubtitleEdits":
        return SubtitleEdits(self.breaks | {word_us}, self.joins - {word_us})

    def join_at(self, word_us: int) -> "SubtitleEdits":
        return SubtitleEdits(self.breaks - {word_us}, self.joins | {word_us})

    def to_dict(self) -> dict:
        return {"breaks": sorted(self.breaks), "joins": sorted(self.joins)}

    @classmethod
    def from_dict(cls, data: dict | None) -> "SubtitleEdits":
        if not isinstance(data, dict):
            return cls()

        def ints(key: str) -> frozenset[int]:
            values = data.get(key)
            return frozenset(int(v) for v in values if isinstance(v, int)) if isinstance(values, list) else frozenset()

        return cls(ints("breaks"), ints("joins"))


@dataclass(frozen=True)
class SubtitleSettings:
    """What a project stores: the style and the user's caption edits."""

    style: SubtitleStyle = field(default_factory=SubtitleStyle)
    edits: SubtitleEdits = field(default_factory=SubtitleEdits)

    def to_dict(self) -> dict:
        return {"style": self.style.to_dict(), "edits": self.edits.to_dict()}

    @classmethod
    def from_dict(cls, data: dict | None) -> "SubtitleSettings":
        data = data if isinstance(data, dict) else {}
        return cls(SubtitleStyle.from_dict(data.get("style")), SubtitleEdits.from_dict(data.get("edits")))


@dataclass(frozen=True)
class CaptionWord:
    text: str
    start_us: int  # output time
    end_us: int
    source_us: int  # source start, the key for edits
    masked: bool = False


@dataclass(frozen=True)
class Caption:
    start_us: int  # output time
    end_us: int
    words: tuple[CaptionWord, ...]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def word_at(self, us: int) -> int | None:
        """Index of the word being spoken at output time ``us``."""
        current = None
        for i, word in enumerate(self.words):
            if word.start_us <= us:
                current = i
        return current


_SENTENCE_END = re.compile(r"[.!?…]$")


def mask_word(text: str, mode: str) -> str:
    letters = [c for c in text if c.isalnum()]
    if mode == MASK_NONE or not letters:
        return text
    core = re.sub(r"[\w-]+", lambda m: (m.group(0)[0] + "*" * (len(m.group(0)) - 1)) if mode == MASK_FIRST
                  else "*" * len(m.group(0)), text, count=1)
    return core


def _muted(timeline: Timeline, start: int, end: int) -> bool:
    """At least half of the word is inside a mute region."""
    length = max(1, end - start)
    covered = sum(max(0, min(end, m.end_us) - max(start, m.start_us)) for m in timeline.mutes)
    return covered * 2 >= length


def _kept(timeline: Timeline, start: int, end: int) -> bool:
    middle = (start + end) // 2
    return any(s.start_us <= middle < s.end_us for s in timeline.segments)


def timed_words(transcript: Transcript, timeline: Timeline, style: SubtitleStyle) -> list[CaptionWord]:
    """Every kept word in output time, masked where it is muted."""
    result: list[CaptionWord] = []
    for segment in transcript.segments:
        words = segment.words
        if not words:
            continue
        for w in words:
            text = w.text.strip()
            if not text or not _kept(timeline, w.start_us, w.end_us):
                continue
            masked = _muted(timeline, w.start_us, w.end_us)
            if masked:
                text = mask_word(text, style.mask)
            if style.uppercase:
                text = text.upper()
            start = timeline.source_to_output(max(w.start_us, timeline.next_kept(w.start_us) or w.start_us))
            end = timeline.source_to_output(w.end_us)
            if end <= start:
                end = start + 1
            result.append(CaptionWord(text, start, end, w.start_us, masked))
    result.sort(key=lambda w: w.start_us)
    return result


def build_captions(
    transcript: Transcript | None,
    timeline: Timeline | None,
    style: SubtitleStyle,
    edits: SubtitleEdits = SubtitleEdits(),
) -> list[Caption]:
    if transcript is None or timeline is None:
        return []
    words = timed_words(transcript, timeline, style)
    groups: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []
    gap = style.gap_split_ms * 1000
    for word in words:
        if current:
            length = len(" ".join(w.text for w in current + [word]))
            forced = word.source_us in edits.breaks
            joined = word.source_us in edits.joins
            full = len(current) >= style.max_words or length > style.max_chars * style.max_lines
            pause = word.start_us - current[-1].end_us >= gap
            sentence = bool(_SENTENCE_END.search(current[-1].text))
            if forced or (not joined and (full or pause or sentence)):
                groups.append(current)
                current = []
        current.append(word)
    if current:
        groups.append(current)

    captions: list[Caption] = []
    min_len = style.min_duration_ms * 1000
    for i, group in enumerate(groups):
        start, end = group[0].start_us, group[-1].end_us
        next_start = groups[i + 1][0].start_us if i + 1 < len(groups) else None
        end = max(end, start + min_len)
        hold = end + 300_000  # keep the last words on screen a little longer
        end = min(hold, next_start) if next_start is not None else hold
        if next_start is not None:
            end = min(end, next_start)
        if timeline is not None:
            end = min(end, timeline.output_us)
        captions.append(Caption(start, max(end, start + 1), tuple(group)))
    return captions


def caption_at(captions: list[Caption], us: int) -> Caption | None:
    for caption in captions:
        if caption.start_us <= us < caption.end_us:
            return caption
    return None


def wrap_lines(words: list[str], max_chars: int, max_lines: int) -> list[list[int]]:
    """Indices of ``words`` per line, balanced, at most ``max_lines`` lines."""
    lines: list[list[int]] = [[]]
    length = 0
    for i, word in enumerate(words):
        extra = len(word) + (1 if lines[-1] else 0)
        if lines[-1] and length + extra > max_chars and len(lines) < max_lines:
            lines.append([])
            length = 0
            extra = len(word)
        lines[-1].append(i)
        length += extra
    return lines

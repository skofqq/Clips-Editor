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
# Animation: none; the caption pops in; the caption pops in and every spoken word jumps;
# the caption slides up and fades in; words appear one by one as they are spoken.
ANIM_NONE, ANIM_POP, ANIM_WORD, ANIM_SLIDE, ANIM_REVEAL = "none", "pop", "word", "slide", "reveal"
ANIMATIONS = (ANIM_NONE, ANIM_POP, ANIM_WORD, ANIM_SLIDE, ANIM_REVEAL)
# Karaoke: only the spoken word is coloured; spoken words stay coloured; the colour
# runs through each word as it is spoken (classic karaoke).
KARAOKE_WORD, KARAOKE_FILL, KARAOKE_SWEEP = "word", "fill", "sweep"
KARAOKE_MODES = (KARAOKE_WORD, KARAOKE_FILL, KARAOKE_SWEEP)
EMOJI_ABOVE, EMOJI_BELOW, EMOJI_INLINE = "above", "below", "inline"
EMOJI_PLACES = (EMOJI_ABOVE, EMOJI_BELOW, EMOJI_INLINE)
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
    hide_punctuation: bool = False  # drop commas and periods (keep ? and !), as many Shorts do
    speaker_colors: bool = True  # after "Find speakers": the 2nd, 3rd… speaker get their own colour
    animation: str = ANIM_NONE
    highlight_box: bool = False  # the spoken word sits on a box in the highlight colour
    emoji: bool = False  # an emoji above captions with a matching word (laughter, fire, money…)
    karaoke_mode: str = KARAOKE_WORD
    dim_upcoming: bool = False  # words not spoken yet are drawn faded
    emphasis: bool = False  # key words (emoji cues, numbers) get their own colour
    emphasis_color: str = "#2bd94f"
    emoji_place: str = EMOJI_ABOVE

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
            animation=self.animation if self.animation in ANIMATIONS else ANIM_NONE,
            karaoke_mode=self.karaoke_mode if self.karaoke_mode in KARAOKE_MODES else KARAOKE_WORD,
            emoji_place=self.emoji_place if self.emoji_place in EMOJI_PLACES else EMOJI_ABOVE,
            position=position,
        )


# Text colour of the 2nd, 3rd… speaker; the first speaker uses the style's colour.
SPEAKER_TEXT_COLORS = ("#7fd4ff", "#ffb86b", "#b4f07a", "#ff8fc8", "#c7a6ff")


def speaker_text_color(style: "SubtitleStyle", speaker: int) -> str:
    if not style.speaker_colors or speaker <= 0:
        return style.color
    return SPEAKER_TEXT_COLORS[(speaker - 1) % len(SPEAKER_TEXT_COLORS)]


PRESETS: dict[str, SubtitleStyle] = {
    # Animated Shorts looks.
    "Karaoke sweep": SubtitleStyle(enabled=True, karaoke_mode=KARAOKE_SWEEP, highlight="#ffd400", max_words=4,
                                   max_chars=20, animation=ANIM_POP, emoji=True),
    "Word by word": SubtitleStyle(enabled=True, animation=ANIM_REVEAL, karaoke_mode=KARAOKE_WORD, max_words=4,
                                  max_chars=20, highlight="#ffd400", emoji=True, emoji_place=EMOJI_INLINE),
    "Key words": SubtitleStyle(enabled=True, animation=ANIM_WORD, highlight="#ffffff", emphasis=True,
                               emphasis_color="#2bd94f", max_words=3, size=84, emoji=True),
    "Fade in, fill": SubtitleStyle(enabled=True, animation=ANIM_SLIDE, karaoke_mode=KARAOKE_FILL,
                                   highlight="#7fd4ff", dim_upcoming=True, max_words=5, max_chars=22,
                                   uppercase=False, size=64),
    "Pop yellow": SubtitleStyle(enabled=True, animation=ANIM_WORD, max_words=3, emoji=True),
    "Green box": SubtitleStyle(enabled=True, animation=ANIM_WORD, highlight="#2bd94f", highlight_box=True,
                               max_words=3, emoji=True),
    "Purple box": SubtitleStyle(enabled=True, animation=ANIM_POP, highlight="#9146ff", highlight_box=True,
                                max_words=4, max_chars=22, emoji=True),
    "Slide box": SubtitleStyle(enabled=True, animation=ANIM_SLIDE, box=True, outline_px=0, uppercase=False,
                               size=60, max_words=5, max_chars=26, highlight="#ffd400", karaoke_mode=KARAOKE_FILL,
                               emoji=True, emoji_place=EMOJI_INLINE),
    "One word pop": SubtitleStyle(enabled=True, animation=ANIM_POP, max_words=1, size=104, max_chars=14,
                                  highlight="#ffffff", emoji=True, emoji_place=EMOJI_BELOW),
    "Red word": SubtitleStyle(enabled=True, animation=ANIM_WORD, highlight="#ff3b3b", max_words=2, size=84),
    # Still looks.
    "Shorts bold": SubtitleStyle(enabled=True),
    "Karaoke green": SubtitleStyle(enabled=True, highlight="#4dff6a", max_words=2),
    "One word": SubtitleStyle(enabled=True, max_words=1, size=96, max_chars=14),
    "Clean": SubtitleStyle(enabled=True, uppercase=False, bold=True, size=60, highlight="#ffffff",
                           karaoke=False, max_words=6, max_chars=32, outline_px=5),
    "Boxed": SubtitleStyle(enabled=True, box=True, outline_px=0, uppercase=False, size=60,
                           max_words=5, max_chars=28, karaoke=False, highlight="#ffffff"),
}


def style_from_preset(preset: SubtitleStyle, current: SubtitleStyle) -> SubtitleStyle:
    """A preset applied to the project: it keeps where the subtitles sit and how swears are written."""
    return replace(preset, enabled=True, position=current.position, mask=current.mask).cleaned()


@dataclass(frozen=True)
class SubtitleEdits:
    breaks: frozenset[int] = frozenset()  # word source start → caption starts here
    joins: frozenset[int] = frozenset()  # word source start → never start a caption here
    # (first word's source start, emoji) — the user's emoji for a caption; "" = no emoji.
    emoji: tuple[tuple[int, str], ...] = ()

    @property
    def empty(self) -> bool:
        return not self.breaks and not self.joins and not self.emoji

    def split_at(self, word_us: int) -> "SubtitleEdits":
        return replace(self, breaks=self.breaks | {word_us}, joins=self.joins - {word_us})

    def join_at(self, word_us: int) -> "SubtitleEdits":
        return replace(self, breaks=self.breaks - {word_us}, joins=self.joins | {word_us})

    def emoji_at(self, word_us: int) -> str | None:
        """The user's emoji for the caption starting at ``word_us``; None = chosen automatically."""
        return dict(self.emoji).get(word_us)

    def with_emoji(self, word_us: int, emoji: str | None) -> "SubtitleEdits":
        """Set (or with None, forget) the emoji of the caption starting at ``word_us``."""
        chosen = {k: v for k, v in self.emoji if k != word_us}
        if emoji is not None:
            chosen[word_us] = emoji
        return replace(self, emoji=tuple(sorted(chosen.items())))

    def to_dict(self) -> dict:
        data = {"breaks": sorted(self.breaks), "joins": sorted(self.joins)}
        if self.emoji:
            data["emoji"] = {str(k): v for k, v in self.emoji}
        return data

    @classmethod
    def from_dict(cls, data: dict | None) -> "SubtitleEdits":
        if not isinstance(data, dict):
            return cls()

        def ints(key: str) -> frozenset[int]:
            values = data.get(key)
            return frozenset(int(v) for v in values if isinstance(v, int)) if isinstance(values, list) else frozenset()

        emoji = data.get("emoji")
        chosen: dict[int, str] = {}
        if isinstance(emoji, dict):
            for key, value in emoji.items():
                if isinstance(value, str) and str(key).lstrip("-").isdigit():
                    chosen[int(key)] = value[:8]
        return cls(ints("breaks"), ints("joins"), tuple(sorted(chosen.items())))


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
    speaker: int = -1
    ends_sentence: bool = False


@dataclass(frozen=True)
class Caption:
    start_us: int  # output time
    end_us: int
    words: tuple[CaptionWord, ...]
    emoji: str | None = None  # the user's emoji ("" = none); None = from the words

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
            sentence_end = bool(_SENTENCE_END.search(text))
            if style.hide_punctuation:
                from core.transcription.cleanup import strip_punctuation

                text = strip_punctuation(text)
            masked = _muted(timeline, w.start_us, w.end_us)
            if masked:
                text = mask_word(text, style.mask)
            if style.uppercase:
                text = text.upper()
            start = timeline.source_to_output(max(w.start_us, timeline.next_kept(w.start_us) or w.start_us))
            end = timeline.source_to_output(w.end_us)
            if end <= start:
                end = start + 1
            result.append(CaptionWord(text, start, end, w.start_us, masked, w.speaker, sentence_end))
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
            sentence = current[-1].ends_sentence or bool(_SENTENCE_END.search(current[-1].text))
            other_voice = word.speaker != current[-1].speaker
            if forced or (not joined and (full or pause or sentence or other_voice)):
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
        captions.append(Caption(start, max(end, start + 1), tuple(group), edits.emoji_at(group[0].source_us)))
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

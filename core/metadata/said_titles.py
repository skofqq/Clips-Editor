"""Title ideas from what is said in the clip.

The transcript is cut into phrases (sentence ends and pauses), and each phrase
is scored the way a viewer would notice it: shouted or exclaimed, a question,
laughter/«жесть»-type words or numbers, words said again and again, short
enough to read at a glance, loud compared with the rest of the clip. Fillers
at the edges are trimmed and swear words are left out (YouTube limits Shorts
with them in the title). The best 3–5 different phrases become ideas for the
clip title.

Everything is local and instant: no model, no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

from core.subtitles.emoji import emoji_for, is_key_word

PAUSE_US = 700_000  # a pause this long ends a phrase
MIN_WORDS, MAX_WORDS = 2, 10
MAX_CHARS = 70
DEFAULT_LIMIT = 5

QUOTE, SHOUT, QUESTION, REACTION, REPEAT, LOUD = "quote", "shout", "question", "reaction", "repeat", "loud"
LABELS = {QUOTE: "What was said", SHOUT: "Exclamation", QUESTION: "Question", REACTION: "Reaction",
          REPEAT: "Said again and again", LOUD: "Loudest moment"}

_FILLERS = {
    "ну", "эм", "эээ", "ээ", "э", "мм", "ммм", "а", "и", "вот", "типа", "короче", "блин", "так", "да", "ага", "угу",
    "ладно", "щас", "сейчас", "бы", "же", "вообще", "просто", "uh", "um", "uhh", "umm", "so",
    "like", "and", "okay", "ok", "well", "yeah", "the", "oh",
}
_WORD = re.compile(r"[\w'’-]+", re.UNICODE)
_END = re.compile(r"([.!?…]+)$")


@dataclass(frozen=True)
class TitleIdea:
    text: str  # ready to be the clip title
    kind: str
    start_us: int
    end_us: int
    score: float

    @property
    def label(self) -> str:
        return LABELS.get(self.kind, LABELS[QUOTE])


@dataclass(frozen=True)
class _Phrase:
    words: tuple[str, ...]
    start_us: int
    end_us: int
    end_mark: str  # "!", "?", "…" or ""


def _norm(word: str) -> str:
    return word.casefold().replace("ё", "е").strip(".,!?…:;\"'«»()—–-")


def _end_mark(word: str) -> str:
    m = _END.search(word.rstrip("\"'»)"))
    if not m:
        return ""
    marks = m.group(1)
    return "?" if "?" in marks else "!" if "!" in marks else "…" if ("…" in marks or "..." in marks) else "."


def _phrases(transcript) -> list[_Phrase]:
    """Sentences, also cut at long pauses, with the times of their words."""
    result: list[_Phrase] = []
    for segment in getattr(transcript, "segments", ()) or ():
        words = [(w.text.strip(), w.start_us, w.end_us) for w in segment.words if w.text.strip()]
        if not words:
            tokens = segment.text.split()
            if not tokens:
                continue
            step = max(1, (segment.end_us - segment.start_us) // len(tokens))
            words = [(t, segment.start_us + i * step, segment.start_us + (i + 1) * step) for i, t in enumerate(tokens)]
        current: list[tuple[str, int, int]] = []
        for i, (text, start, end) in enumerate(words):
            if current and start - current[-1][2] >= PAUSE_US:
                result.append(_Phrase(tuple(t for t, _s, _e in current), current[0][1], current[-1][2], ""))
                current = []
            current.append((text, start, end))
            mark = _end_mark(text)
            if mark and mark != "…" or i == len(words) - 1:
                result.append(_Phrase(tuple(t for t, _s, _e in current), current[0][1], current[-1][2], mark))
                current = []
    return result


def _trim(words: list[str]) -> list[str]:
    """Drop fillers at both ends ("ну вот это жесть вообще" → "это жесть")."""
    while words and _norm(words[0]) in _FILLERS:
        words = words[1:]
    while words and _norm(words[-1]) in _FILLERS:
        words = words[:-1]
    return words


def _swear(word: str, dictionary) -> bool:
    if "*" in word:
        return True
    if dictionary is None:
        return False
    try:
        return dictionary.match(word) is not None
    except Exception:  # noqa: BLE001 - a broken custom entry must not break the ideas
        return False


def format_title(words: Iterable[str], mark: str = "") -> str:
    """Words as a clip title: inner punctuation kept, the first letter capital, ``!``/``?`` kept at the end."""
    text = " ".join(words).strip()
    text = re.sub(r"[\s,.;:—–-]+$", "", text)
    text = re.sub(r"[.!?…]+$", "", text).strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if mark in ("!", "?"):
        text += mark
    elif mark == "…":
        text += "…"
    return text


def _repeats(words: list[str]) -> int:
    """How many times a word is said again right after itself ("нет нет нет" → 2)."""
    normed = [_norm(w) for w in words]
    return sum(1 for a, b in zip(normed, normed[1:]) if a and a == b)


def title_ideas(transcript, dictionary=None, loudness: Callable[[int, int], float] | None = None,
                limit: int = DEFAULT_LIMIT) -> list[TitleIdea]:
    """The most striking phrases of the clip, best first, at most ``limit``.

    ``loudness(start_us, end_us)`` gives 0..1 for a phrase (peak level); optional."""
    candidates: list[TitleIdea] = []
    phrases = _phrases(transcript)
    levels = [loudness(p.start_us, p.end_us) for p in phrases] if loudness is not None else []
    typical = sorted(levels)[len(levels) // 2] if levels else 0.0
    for index, phrase in enumerate(phrases):
        if any(_swear(w, dictionary) for w in phrase.words):
            # Drop the swear and what is around it only when the rest still makes a phrase.
            words = [w for w in phrase.words if not _swear(w, dictionary)]
            if len(words) < len(phrase.words) - 1:
                continue
        else:
            words = list(phrase.words)
        words = _trim(words)
        meaningful = [w for w in words if _norm(w) and _norm(w) not in _FILLERS]
        if len(words) < MIN_WORDS or len(meaningful) < 1 or len(words) > MAX_WORDS + 4:
            continue
        if len(words) > MAX_WORDS:
            words = _trim(words[:MAX_WORDS])
            mark = "…"
        else:
            mark = phrase.end_mark
        text = format_title(words, mark)
        if not text or len(text) > MAX_CHARS or len(_WORD.findall(text)) < MIN_WORDS:
            continue
        score, kind = 1.0, QUOTE
        n = len(words)
        score += 1.2 if 3 <= n <= 7 else 0.4 if n <= MAX_WORDS else 0.0
        score += min(1.0, len(meaningful) / max(1, n))  # few fillers
        if mark == "!":
            score += 2.0
            kind = SHOUT
        elif mark == "?":
            score += 1.4
            kind = QUESTION
        keys = sum(1 for w in words if is_key_word(w))
        if keys:
            score += min(2.0, 1.4 * keys)
            if kind == QUOTE:
                kind = REACTION
        if emoji_for(words):
            score += 0.4
        repeats = _repeats(words)
        if repeats:
            score += min(1.5, 0.8 * repeats)
            if kind == QUOTE:
                kind = REPEAT
        if any(w.isupper() and len(w) > 2 for w in words):
            score += 0.8
        if levels:
            level = levels[index]
            if typical > 0 and level > typical * 1.25:
                score += min(2.0, 2.5 * (level / typical - 1.0))
                if level >= max(levels) and kind == QUOTE:
                    kind = LOUD
        candidates.append(TitleIdea(text, kind, phrase.start_us, phrase.end_us, round(score, 3)))
    candidates.sort(key=lambda c: (-c.score, c.start_us))
    chosen: list[TitleIdea] = []
    seen: set[str] = set()
    for idea in candidates:
        key = " ".join(_norm(w) for w in idea.text.split())
        if key in seen or any(key in other or other in key for other in seen):
            continue
        seen.add(key)
        chosen.append(idea)
        if len(chosen) >= limit:
            break
    return chosen


def waveform_loudness(waveform) -> Callable[[int, int], float] | None:
    """Phrase loudness from a core.audio.waveform.Waveform (peak level 0..1), or None."""
    if waveform is None or not len(getattr(waveform, "peaks", ())):
        return None
    peaks, rate = waveform.peaks, waveform.peaks_per_second

    def level(start_us: int, end_us: int) -> float:
        lo = max(0, int(start_us / 1e6 * rate))
        hi = min(len(peaks), max(lo + 1, int(end_us / 1e6 * rate) + 1))
        if lo >= len(peaks):
            return 0.0
        part = sorted(int(x) for x in peaks[lo:hi])
        # The upper quartile: a shout, not a single click.
        return part[min(len(part) - 1, (3 * len(part)) // 4)] / 255.0

    return level


__all__ = ["TitleIdea", "title_ideas", "format_title", "waveform_loudness", "LABELS"]

"""Text cleanup after recognition: replacement rules and sentence capitals.

Rules are the user's own fixes for words Whisper keeps getting wrong
("скоф → Skof", "кс го → CS:GO"). One rule per line::

    скоф = Skof
    кс го -> CS:GO
    гг → GG

Matching ignores case, "ё"/"е" and punctuation, and works on whole words;
a phrase of several words is matched across consecutive words. The
replacement keeps the punctuation around the original ("скоф," becomes
"Skof,") and the words keep their timing: several words replaced by one
take their combined span, one word replaced by several shares its span.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from core.transcription.model import Segment, Transcript, Word

_ARROW = re.compile(r"\s*(?:->|→|=>|=)\s*")
_EDGE = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)
_SENTENCE_END = re.compile(r"[.!?…]['\")»]*$")


@dataclass(frozen=True)
class Rule:
    find: tuple[str, ...]  # normalised words
    replace: str


def _norm(word: str) -> str:
    return re.sub(r"[^\w]", "", word.casefold().replace("ё", "е"))


def parse_rules(text: str) -> list[Rule]:
    rules: list[Rule] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = _ARROW.split(line, maxsplit=1)
        if len(parts) != 2:
            continue
        find = tuple(w for w in (_norm(x) for x in parts[0].split()) if w)
        target = " ".join(parts[1].split())
        if find and target:
            rules.append(Rule(find, target))
    # Longer phrases first, so "кс го" wins over "го".
    return sorted(rules, key=lambda r: -len(r.find))


def _replace_words(words: list[Word], rules: list[Rule]) -> tuple[list[Word], int]:
    out: list[Word] = []
    count = 0
    i = 0
    keys = [_norm(w.text) for w in words]
    while i < len(words):
        rule = next((r for r in rules if tuple(keys[i:i + len(r.find)]) == r.find), None)
        if rule is None:
            out.append(words[i])
            i += 1
            continue
        span = words[i:i + len(rule.find)]
        lead = _EDGE.match(span[0].text).group(1)
        trail = _EDGE.match(span[-1].text).group(3)
        tokens = rule.replace.split(" ")
        tokens[0] = lead + tokens[0]
        tokens[-1] = tokens[-1] + trail
        start, end = span[0].start_us, span[-1].end_us
        exact = all(w.exact for w in span)
        base = replace(span[0], end_us=end, probability=min(w.probability for w in span),
                       timing=span[0].timing if exact else span[-1].timing)
        if len(tokens) == 1:
            out.append(replace(base, text=tokens[0]))
        else:
            total = sum(len(t) for t in tokens)
            cursor = start
            for k, token in enumerate(tokens):
                stop = end if k == len(tokens) - 1 else cursor + (end - start) * len(token) // total
                out.append(replace(base, start_us=cursor, end_us=max(cursor + 1, stop), text=token))
                cursor = stop
        count += 1
        i += len(rule.find)
    return out, count


def _capitalise(text: str) -> str:
    for k, ch in enumerate(text):
        if ch.isalpha():
            return text[:k] + ch.upper() + text[k + 1:] if ch.islower() else text
        if ch.isdigit():
            return text
    return text


def clean_transcript(transcript: Transcript, rules: list[Rule] | None = None,
                     capitalise: bool = False) -> tuple[Transcript, int]:
    """Apply ``rules`` and, optionally, capital letters at sentence starts.

    Returns the new transcript and how many replacements were made.
    """
    rules = rules or []
    total = 0
    segments: list[Segment] = []
    new_sentence = True
    for seg in transcript.segments:
        words = list(seg.words)
        if rules:
            words, n = _replace_words(words, rules)
            total += n
        if capitalise:
            for k, w in enumerate(words):
                if new_sentence:
                    text = _capitalise(w.text)
                    if text != w.text:
                        words[k] = replace(w, text=text)
                new_sentence = bool(_SENTENCE_END.search(w.text))
        if words != list(seg.words):
            seg = replace(seg, words=tuple(words), text=" ".join(w.text for w in words))
        segments.append(seg)
    return replace(transcript, segments=tuple(segments)), total


def strip_punctuation(text: str) -> str:
    """For subtitles: drop commas, periods, colons, semicolons and dashes at word edges; keep ? and !."""
    match = _EDGE.match(text)
    core, trail = match.group(2), match.group(3)
    keep = "".join(c for c in trail if c in "?!")
    return (core + keep) or text

"""Translating a transcript: each phrase is translated, then its new words are
spread over the time the original words took (longer words get more time),
so captions, karaoke and censoring keep working on the translation.

A translated transcript has ``model`` = ``"translate:<direction>"`` and the
target ``language``. The user can correct any phrase afterwards.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Callable

from core.transcription.model import SEGMENT_TIMING, Segment, Transcript, Word

TRANSLATED_PREFIX = "translate:"
_SPACE = re.compile(r"\s+")

TranslateLines = Callable[..., list[str]]


def is_translation(transcript: Transcript | None) -> bool:
    return transcript is not None and transcript.model.startswith(TRANSLATED_PREFIX)


def _phrase(segment: Segment) -> str:
    return " ".join(w.text for w in segment.words) if segment.words else segment.text


def spread_words(segment: Segment, text: str) -> tuple[Word, ...]:
    """Words of ``text`` timed over ``segment``'s original words, by length."""
    tokens = [t for t in _SPACE.split(text.strip()) if t]
    if not tokens:
        return ()
    if segment.words:
        start, end = segment.words[0].start_us, segment.words[-1].end_us
        speakers = [w.speaker for w in segment.words if w.speaker >= 0]
    else:
        start, end, speakers = segment.start_us, segment.end_us, []
    end = max(end, start + len(tokens))
    speaker = max(set(speakers), key=speakers.count) if speakers else -1
    weights = [len(t) + 1 for t in tokens]
    total = sum(weights)
    words, cursor, acc = [], start, 0
    for token, weight in zip(tokens, weights):
        acc += weight
        stop = start + (end - start) * acc // total
        words.append(Word(cursor, max(cursor + 1, stop), token, 1.0, SEGMENT_TIMING, speaker))
        cursor = stop
    return tuple(words)


def with_phrase(transcript: Transcript, segment_id: int, text: str) -> Transcript:
    """The translation with one phrase corrected by the user (words re-timed)."""
    segments = []
    for s in transcript.segments:
        if s.id == segment_id:
            text = _SPACE.sub(" ", text).strip()
            s = replace(s, text=text, words=spread_words(s, text), edited=True)
        segments.append(s)
    return replace(transcript, segments=tuple(segments))


def translate_transcript(transcript: Transcript, translate: TranslateLines, direction: str,
                         progress=None, cancel=None) -> Transcript:
    """A new transcript in the target language of ``direction`` ("ru-en")."""
    target = direction.split("-")[-1]
    lines = [_phrase(s) for s in transcript.segments]
    translated = translate(lines, progress=progress, cancel=cancel)
    segments = []
    for segment, text in zip(transcript.segments, translated):
        # Spread over the original words first; the segment keeps the original text's timing.
        segments.append(replace(segment, text=text, words=spread_words(segment, text), edited=False))
    return Transcript(
        language=target,
        language_probability=1.0,
        model=TRANSLATED_PREFIX + direction,
        segments=tuple(segments),
        duration_us=transcript.duration_us,
    )


def translate_file(models_dir, transcript: Transcript, direction: str, progress=None, cancel=None) -> Transcript:
    """Download the model if needed, then translate (runs on a worker thread)."""
    from core.translation.engine import Translator
    from core.translation.models import download_model

    report = progress or (lambda *_a: None)
    folder = download_model(models_dir, direction, lambda f, m: report(f * 0.5, m), cancel)
    report(0.5, "Loading the translation model…")
    translator = Translator(folder)
    where = "GPU" if translator.device == "cuda" else "CPU"
    return translate_transcript(
        transcript, translator.translate, direction,
        progress=lambda f, _m="": report(0.5 + 0.5 * f, f"Translating on the {where}… {int(f * 100)} %"),
        cancel=cancel,
    )

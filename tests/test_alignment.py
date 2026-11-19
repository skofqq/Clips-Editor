"""Word timing refinement against synthetic speech-like bursts."""

from __future__ import annotations

import numpy as np

from core.transcription.alignment import refine_segments
from core.transcription.model import SEGMENT_TIMING, Segment, Word

RATE = 16000
MS = 1000


def _audio(bursts, length_s=4.0, seed=0):
    rng = np.random.default_rng(seed)
    audio = 0.002 * rng.standard_normal(int(length_s * RATE))  # room noise
    t = np.arange(audio.size) / RATE
    for start, end in bursts:
        a, b = int(start * RATE / 1000), int(end * RATE / 1000)
        audio[a:b] += 0.3 * np.sin(2 * np.pi * 220 * t[a:b]) * (1 + 0.3 * np.sin(2 * np.pi * 5 * t[a:b]))
    return audio.astype(np.float32)


def _seg(words):
    ws = tuple(Word(s * MS, e * MS, txt) for s, e, txt in words)
    return (Segment(1, ws[0].start_us, ws[-1].end_us, " ".join(w.text for w in ws), ws),)


def test_edges_snap_to_the_voice():
    # True words: 500–800, 1200–1500 ms, separated by silence.
    audio = _audio([(500, 800), (1200, 1500)])
    # Whisper-like errors: early start in silence, cut tail, late start, long tail.
    seg = _seg([(400, 720, "раз"), (1290, 1600, "два")])
    out = refine_segments(seg, audio, RATE)[0].words
    for word, (start, end) in zip(out, [(500, 800), (1200, 1500)]):
        assert abs(word.start_us / MS - start) <= 25, word
        assert abs(word.end_us / MS - end) <= 25, word
    assert out[0].end_us <= out[1].start_us


def test_joined_words_split_at_the_quietest_point():
    # Two bursts with a short dip at 1000 ms; Whisper puts the boundary at 1080.
    audio = _audio([(600, 990), (1010, 1400)])
    seg = _seg([(600, 1080, "один"), (1080, 1400, "два")])
    a, b = refine_segments(seg, audio, RATE)[0].words
    assert abs(a.end_us / MS - 1000) <= 20 and a.end_us == b.start_us


def test_shift_is_limited_and_rough_words_untouched():
    audio = _audio([(1500, 1800)])
    seg = _seg([(500, 700, "далеко")])  # nothing voiced nearby: stays roughly where it was
    w = refine_segments(seg, audio, RATE)[0].words[0]
    assert abs(w.start_us / MS - 500) <= 150 and abs(w.end_us / MS - 700) <= 150
    rough = (Segment(1, 400 * MS, 900 * MS, "x", (Word(400 * MS, 900 * MS, "x", 0.0, SEGMENT_TIMING),)),)
    assert refine_segments(rough, _audio([(500, 800)]), RATE) == rough


def test_silence_and_offsets():
    silent = np.zeros(RATE * 2, np.float32)
    seg = _seg([(400, 700, "тишина")])
    assert refine_segments(seg, silent, RATE) == seg
    # Audio that starts at 10 s of clip time (re-transcribing a range).
    audio = _audio([(500, 800)])
    seg = _seg([(10_420, 10_760, "раз")])
    w = refine_segments(seg, audio, RATE, offset_us=10_000_000)[0].words[0]
    assert abs(w.start_us / MS - 10_500) <= 25 and abs(w.end_us / MS - 10_800) <= 25


def test_transcribe_file_refines_when_asked(sample_clips, ffmpeg_paths, tmp_path, monkeypatch):
    from core.transcription import alignment, engine as engine_mod
    from core.transcription.engine import TranscribeOptions, WhisperEngine
    from core.transcription.model import Transcript

    engine = WhisperEngine(tmp_path)
    monkeypatch.setattr(engine, "transcribe", lambda a, o, p=None, c=None: Transcript("ru", 1, "s", _seg([(100, 300, "x")]), 0))
    calls = []
    real = alignment.refine_segments
    monkeypatch.setattr(alignment, "refine_segments", lambda *a, **k: calls.append(a) or real(*a, **k))
    engine_mod.transcribe_file(engine, sample_clips["mp4"], ffmpeg_paths.ffmpeg, TranscribeOptions())
    assert len(calls) == 1
    engine_mod.transcribe_file(engine, sample_clips["mp4"], ffmpeg_paths.ffmpeg, TranscribeOptions(refine_timing=False))
    assert len(calls) == 1

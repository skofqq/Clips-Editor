"""Word timing refinement: snap Whisper's word edges to the audio.

Whisper's word timestamps come from its attention and are often 50–200 ms
off: a word "starts" in the pause before it, or its tail is cut. Muting
or captioning such a word then leaks a syllable or clips the next one.

This pass looks at the loudness of the audio Whisper heard (16 kHz mono,
10 ms frames) and moves each edge, by at most ``MAX_SHIFT_MS``:

* a start that lies in silence moves forward to where the voice begins,
  a start inside sound moves back to the onset;
* an end inside sound is extended to where the voice stops, an end in
  silence moves back to the last voiced frame;
* where two words touch, the shared edge moves to the quietest point
  between them.

Words never overlap and never swap order. Words without word-level timing
(marked ``≈``) are left alone.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from core.transcription.model import WORD_TIMING, Segment

FRAME_US = 10_000
MAX_SHIFT_MS = 150
MIN_WORD_US = 40_000
TOUCH_US = 30_000  # words closer than this are treated as joined


def frame_levels(audio: np.ndarray, rate: int) -> np.ndarray:
    """Loudness per 10 ms frame in dB (20 ms window), lightly smoothed."""
    hop = rate * FRAME_US // 1_000_000
    if audio.size < hop:
        return np.zeros(0, dtype=np.float32)
    frames = audio.size // hop
    power = np.square(audio[: frames * hop].astype(np.float64)).reshape(frames, hop).mean(axis=1)
    power = np.convolve(np.pad(power, (1, 0), mode="edge"), np.ones(2) / 2, mode="valid")  # 20 ms window
    db = 10 * np.log10(power + 1e-10)
    return np.convolve(np.pad(db, 1, mode="edge"), np.ones(3) / 3, mode="valid").astype(np.float32)


def voice_threshold(levels: np.ndarray) -> float:
    """Between the noise floor and typical speech loudness."""
    floor = float(np.percentile(levels, 10))
    loud = float(np.percentile(levels, 95))
    return floor + 0.3 * max(0.0, loud - floor)


def refine_segments(segments: tuple[Segment, ...], audio: np.ndarray, rate: int,
                    offset_us: int = 0) -> tuple[Segment, ...]:
    """Return ``segments`` with word edges snapped to ``audio`` (which starts at ``offset_us``)."""
    levels = frame_levels(audio, rate)
    if levels.size < 10 or float(levels.max() - np.percentile(levels, 10)) < 6.0:
        return segments  # silence or flat noise: nothing to snap to
    voiced = levels >= voice_threshold(levels)
    n = levels.size
    shift = MAX_SHIFT_MS * 1000 // FRAME_US

    def frame(us: int) -> int:
        return min(n - 1, max(0, (us - offset_us) // FRAME_US))

    def time(f: int) -> int:
        return offset_us + f * FRAME_US

    # Flatten (keeping positions) so neighbours across segments are respected.
    flat = [(si, wi, w) for si, seg in enumerate(segments) for wi, w in enumerate(seg.words)]
    starts = [w.start_us for _, _, w in flat]
    ends = [w.end_us for _, _, w in flat]
    exact = [w.timing == WORD_TIMING for _, _, w in flat]

    for i in range(len(flat)):
        if not exact[i]:
            continue
        prev_end = ends[i - 1] if i > 0 else offset_us
        next_start = starts[i + 1] if i + 1 < len(flat) else time(n)
        joined_prev = i > 0 and exact[i - 1] and starts[i] - ends[i - 1] < TOUCH_US
        # ---- start
        f = frame(starts[i])
        if joined_prev:
            lo, hi = frame(max(prev_end - shift * FRAME_US, starts[i - 1] + MIN_WORD_US)), \
                frame(min(starts[i] + shift * FRAME_US, ends[i] - MIN_WORD_US))
            if hi > lo:
                quiet = lo + int(np.argmin(levels[lo:hi + 1]))
                boundary = time(quiet)
                ends[i - 1] = boundary
                starts[i] = boundary
        elif voiced[f]:
            stop = frame(max(prev_end, starts[i] - shift * FRAME_US))
            while f > stop and voiced[f - 1]:
                f -= 1
            starts[i] = max(prev_end, time(f))
        else:
            stop = frame(min(ends[i] - MIN_WORD_US, starts[i] + shift * FRAME_US))
            g = f
            while g < stop and not voiced[g]:
                g += 1
            if voiced[g]:
                starts[i] = time(g)
        # ---- end
        joined_next = i + 1 < len(flat) and exact[i + 1] and next_start - ends[i] < TOUCH_US
        if joined_next:
            continue  # the next word's start sets this edge
        f = frame(ends[i] - 1)
        if voiced[f]:
            stop = frame(min(next_start, ends[i] + shift * FRAME_US) - 1)
            while f < stop and voiced[f + 1]:
                f += 1
            ends[i] = min(next_start, time(f + 1))
        else:
            stop = frame(max(starts[i] + MIN_WORD_US, ends[i] - shift * FRAME_US))
            g = f
            while g > stop and not voiced[g]:
                g -= 1
            if voiced[g]:
                ends[i] = time(g + 1)
        if ends[i] - starts[i] < MIN_WORD_US:
            ends[i] = min(starts[i] + MIN_WORD_US, next_start) if next_start > starts[i] else starts[i] + 1

    # Rebuild, keeping order and segment bounds consistent.
    words: dict[int, list] = {}
    for k, (si, _wi, w) in enumerate(flat):
        start, end = starts[k], max(starts[k] + 1, ends[k])
        words.setdefault(si, []).append(replace(w, start_us=start, end_us=end) if exact[k] else w)
    result = []
    for si, seg in enumerate(segments):
        ws = tuple(words.get(si, ()))
        if ws:
            seg = replace(seg, words=ws, start_us=ws[0].start_us, end_us=ws[-1].end_us)
        result.append(seg)
    return tuple(result)

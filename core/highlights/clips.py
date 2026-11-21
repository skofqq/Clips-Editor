"""Save ticked moments of a long recording as separate clips.

Each moment becomes its own short video next to the source (the source is
never changed), so every Short gets its own edit, captions, title and
upload history. The cut is re-encoded (GPU when available) so that it
starts exactly on the chosen frame; the transcript of the long recording
is cut to match, so the new clips do not need to be transcribed again.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Callable

from core.media.ffmpeg_locator import no_window_kwargs
from core.render.encoders import video_args
from core.render.settings import RenderSettings

log = logging.getLogger(__name__)

US = 1_000_000
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


class ClipCancelled(Exception):
    pass


class ClipError(RuntimeError):
    pass


def stamp(us: int) -> str:
    s = max(0, us) // US
    return f"{s // 3600}-{s % 3600 // 60:02d}-{s % 60:02d}"


def clip_name(source: Path, highlight, number: int) -> str:
    """``Stream – 01 – 1-02-03 – first words.mp4`` (Windows-safe)."""
    words = _BAD.sub(" ", highlight.quote or "").replace("…", "").strip()
    words = re.sub(r"\s+", " ", words)[:40].rsplit(" ", 1)[0] if len(words) > 40 else words
    parts = [source.stem[:60], f"{number:02d}", stamp(highlight.start_us)] + ([words] if words else [])
    stem = re.sub(r"\s+", " ", _BAD.sub(" ", " – ".join(parts))).strip(" .")
    return f"{stem}.mp4"


def clip_command(ffmpeg: Path, source: Path, target: Path, start_us: int, end_us: int,
                 encoder: str = "libx264", fps: float = 30.0) -> list[str]:
    settings = RenderSettings(quality=18)
    return [
        str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-progress", "pipe:1",
        "-ss", f"{start_us / US:.3f}", "-i", str(source), "-t", f"{(end_us - start_us) / US:.3f}",
        "-map", "0:v:0?", "-map", "0:a:0?",
        *video_args(encoder, settings, fps or 30.0), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(target),
    ]


def slice_transcript(transcript, start_us: int, end_us: int):
    """The words said between start and end, with times from the start of the cut."""
    if transcript is None:
        return None
    segments = []
    for seg in transcript.segments:
        if seg.end_us <= start_us or seg.start_us >= end_us:
            continue
        words = tuple(replace(w, start_us=max(0, w.start_us - start_us), end_us=min(end_us, w.end_us) - start_us)
                      for w in seg.words if w.end_us > start_us and w.start_us < end_us)
        if seg.words and not words:
            continue
        text = " ".join(w.text for w in words) if words and len(words) != len(seg.words) else seg.text
        segments.append(replace(seg, id=len(segments) + 1, start_us=max(0, seg.start_us - start_us),
                                end_us=min(end_us, seg.end_us) - start_us, text=text, words=words))
    check = tuple(replace(w, start_us=w.start_us - start_us, end_us=w.end_us - start_us)
                  for w in transcript.check_words if start_us <= w.start_us and w.end_us <= end_us)
    return replace(transcript, segments=tuple(segments), duration_us=end_us - start_us, check_words=check)


def save_clips(
    ffmpeg: Path,
    source: Path,
    highlights: list,
    folder: Path,
    *,
    encoder: str = "libx264",
    fps: float = 30.0,
    progress: Callable[[float, str], None] | None = None,
    cancel=None,
    run: Callable | None = None,
) -> list[tuple[Path, object]]:
    """Cut every highlight into ``folder``. Returns (file, highlight) pairs.

    A file that already exists (same moment saved before) is reused."""
    folder.mkdir(parents=True, exist_ok=True)
    total_us = sum(h.end_us - h.start_us for h in highlights) or 1
    done_us = 0
    out: list[tuple[Path, object]] = []
    for number, h in enumerate(sorted(highlights, key=lambda h: h.start_us), 1):
        if cancel is not None and cancel.is_set():
            raise ClipCancelled()
        target = folder / clip_name(source, h, number)
        if not (target.is_file() and target.stat().st_size > 0):
            tmp = target.with_name(target.stem + ".part.mp4")
            cmd = clip_command(ffmpeg, source, tmp, h.start_us, h.end_us, encoder, fps)
            label = f"Saving clip {number} of {len(highlights)}…"
            try:
                (run or _run)(cmd, lambda us, base=done_us: progress and progress(
                    min(0.99, (base + us) / total_us), label), cancel)
            except ClipCancelled:
                tmp.unlink(missing_ok=True)
                raise
            tmp.replace(target)
        done_us += h.end_us - h.start_us
        out.append((target, h))
        if progress:
            progress(min(1.0, done_us / total_us), f"Saved {number} of {len(highlights)}")
    return out


def _run(cmd: list[str], on_progress: Callable[[int], None], cancel) -> None:
    log.info("Cutting moment: %s", " ".join(cmd))
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               encoding="utf-8", errors="replace", **no_window_kwargs())
    assert process.stdout is not None
    try:
        for line in process.stdout:
            if cancel is not None and cancel.is_set():
                process.kill()
                raise ClipCancelled()
            if line.startswith("out_time_us="):
                try:
                    on_progress(max(0, int(line.split("=", 1)[1])))
                except ValueError:
                    pass
        _, err = process.communicate()
    finally:
        if process.poll() is None:
            process.kill()
    if process.returncode != 0:
        target = Path(cmd[-1])
        target.unlink(missing_ok=True)
        raise ClipError(f"FFmpeg could not save the clip:\n{(err or '').strip()[-500:]}")

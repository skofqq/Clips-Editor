"""Subtitle files: SubRip (.srt) and WebVTT (.vtt), in output time."""

from __future__ import annotations

from pathlib import Path

from core.subtitles.model import Caption


def _ts(us: int, sep: str) -> str:
    ms = max(0, int(round(us / 1000)))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(captions: list[Caption]) -> str:
    blocks = []
    for n, c in enumerate(captions, start=1):
        blocks.append(f"{n}\n{_ts(c.start_us, ',')} --> {_ts(c.end_us, ',')}\n{c.text}\n")
    return "\n".join(blocks)


def to_vtt(captions: list[Caption]) -> str:
    blocks = ["WEBVTT\n"]
    for c in captions:
        blocks.append(f"{_ts(c.start_us, '.')} --> {_ts(c.end_us, '.')}\n{c.text}\n")
    return "\n".join(blocks)


def write_subtitles(path: Path, captions: list[Caption]) -> Path:
    path = Path(path)
    text = to_vtt(captions) if path.suffix.lower() == ".vtt" else to_srt(captions)
    path.write_text(text, encoding="utf-8-sig" if path.suffix.lower() == ".srt" else "utf-8")
    return path

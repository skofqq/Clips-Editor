"""Waveform peaks for the timeline, extracted with FFmpeg and cached on disk.

The audio is decoded to mono 8 kHz PCM and reduced to one peak value per
10 ms. A 10-minute clip becomes 60 000 bytes, so drawing stays fast at
any zoom level. The source file is only read.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core.media.errors import MediaError
from core.media.ffmpeg_locator import no_window_kwargs

log = logging.getLogger(__name__)

DECODE_RATE = 8000
PEAKS_PER_SECOND = 100
_BUCKET = DECODE_RATE // PEAKS_PER_SECOND
_CACHE_VERSION = 1


class WaveformError(MediaError):
    pass


@dataclass(frozen=True)
class Waveform:
    peaks: np.ndarray  # uint8, 0..255 absolute peak per bucket
    peaks_per_second: int = PEAKS_PER_SECOND

    @property
    def duration_s(self) -> float:
        return len(self.peaks) / self.peaks_per_second

    def columns(self, start_s: float, end_s: float, width: int) -> np.ndarray:
        """Peak (0..1) for each of ``width`` pixel columns covering the range.

        Positions outside the audio yield 0.
        """
        if width <= 0:
            return np.zeros(0, dtype=np.float32)
        out = np.zeros(width, dtype=np.float32)
        if end_s <= start_s or not len(self.peaks):
            return out
        edges = np.linspace(start_s, end_s, width + 1) * self.peaks_per_second
        lo = np.floor(edges[:-1]).astype(np.int64)
        hi = np.maximum(np.ceil(edges[1:]).astype(np.int64), lo + 1)
        n = len(self.peaks)
        valid = (hi > 0) & (lo < n)
        if not valid.any():
            return out
        lo_c = np.clip(lo[valid], 0, n - 1)
        hi_c = np.clip(hi[valid], 1, n)
        # Prefix maxima are not possible, so reduce each column separately,
        # but vectorised through reduceat on interleaved boundaries.
        bounds = np.empty(lo_c.size * 2, dtype=np.int64)
        bounds[0::2] = lo_c
        bounds[1::2] = np.minimum(hi_c, n - 1)
        reduced = np.maximum.reduceat(self.peaks, bounds)[0::2]
        # reduceat excludes the upper bound; include the last sample explicitly.
        reduced = np.maximum(reduced, self.peaks[hi_c - 1])
        out[valid] = reduced.astype(np.float32) / 255.0
        return out


def peaks_from_pcm(samples: np.ndarray) -> np.ndarray:
    """Reduce int16 mono samples to uint8 peaks (one per bucket)."""
    if samples.size == 0:
        return np.zeros(0, dtype=np.uint8)
    pad = (-samples.size) % _BUCKET
    if pad:
        samples = np.concatenate([samples, np.zeros(pad, dtype=np.int16)])
    magnitude = np.abs(samples.astype(np.int32)).reshape(-1, _BUCKET).max(axis=1)
    return np.minimum(magnitude >> 7, 255).astype(np.uint8)


def extract_waveform(path: Path, ffmpeg: Path, audio_stream: int = 0) -> Waveform:
    """Decode the audio track and build peaks. Raises :class:`WaveformError`."""
    cmd = [
        str(ffmpeg), "-v", "error", "-nostdin", "-i", str(path),
        "-map", f"0:a:{audio_stream}", "-vn", "-ac", "1", "-ar", str(DECODE_RATE),
        "-f", "s16le", "-",
    ]
    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **no_window_kwargs()
        )
    except OSError as exc:
        raise WaveformError(f"Could not start FFmpeg: {exc}") from exc

    chunks: list[np.ndarray] = []
    leftover = b""
    chunk_bytes = _BUCKET * 2 * 4096
    assert process.stdout is not None
    while True:
        data = process.stdout.read(chunk_bytes)
        if not data:
            break
        data = leftover + data
        usable = len(data) - (len(data) % (_BUCKET * 2))
        leftover = data[usable:]
        if usable:
            chunks.append(peaks_from_pcm(np.frombuffer(data[:usable], dtype="<i2")))
    if len(leftover) >= 2:
        tail = leftover[: len(leftover) - (len(leftover) % 2)]
        chunks.append(peaks_from_pcm(np.frombuffer(tail, dtype="<i2")))
    stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    code = process.wait()
    if code != 0:
        message = stderr.strip().splitlines()[-1] if stderr.strip() else f"exit code {code}"
        raise WaveformError(f"Could not read audio from {path.name}: {message}")
    peaks = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.uint8)
    return Waveform(peaks)


def cache_file(path: Path, cache_dir: Path, audio_stream: int = 0) -> Path:
    """Cache name derived from the path, size and modification time, so the
    waveform is rebuilt automatically when the source changes."""
    stat = path.stat()
    key = f"{_CACHE_VERSION}|{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{audio_stream}|{PEAKS_PER_SECOND}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return cache_dir / "waveforms" / f"{digest}.npy"


def load_waveform(path: Path, ffmpeg: Path, cache_dir: Path, audio_stream: int = 0) -> Waveform:
    """Return the cached waveform, building and caching it when needed."""
    path = Path(path)
    target = cache_file(path, cache_dir, audio_stream)
    if target.is_file():
        try:
            peaks = np.load(target, allow_pickle=False)
            if peaks.dtype == np.uint8 and peaks.ndim == 1:
                return Waveform(peaks)
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable waveform cache %s: %s", target, exc)
    waveform = extract_waveform(path, ffmpeg, audio_stream)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with tmp.open("wb") as handle:
            np.save(handle, waveform.peaks, allow_pickle=False)
        tmp.replace(target)
    except OSError as exc:
        log.warning("Could not write waveform cache: %s", exc)
    return waveform

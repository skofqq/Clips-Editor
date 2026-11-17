"""Local speech recognition with faster-whisper.

* Audio is decoded by our FFmpeg to 16 kHz mono float32, so the original
  file is only read and no extra decoder is needed.
* Silero VAD (bundled with faster-whisper) skips non-speech.
* Word timestamps are requested; segments where the model gives none are
  kept with ``segment`` timing (see core.transcription.model).
* GPU (CUDA) is used when available and falls back to CPU if the CUDA
  libraries are missing or fail to load.

Nothing here touches Qt; progress and cancellation go through callbacks.
"""

from __future__ import annotations

import glob
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from core.media.errors import MediaError
from core.media.ffmpeg_locator import no_window_kwargs
from core.timeline.timebase import seconds_to_us
from core.transcription.model import SEGMENT_TIMING, WORD_TIMING, Segment, Transcript, Word

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo")
DEFAULT_MODEL = "small"
AUTO = "auto"


class TranscriptionError(MediaError):
    pass


class TranscriptionCancelled(Exception):
    pass


@dataclass(frozen=True)
class TranscribeOptions:
    model: str = DEFAULT_MODEL
    language: str = AUTO  # "auto", "ru" or "en"
    device: str = AUTO  # "auto", "cuda" or "cpu"
    vad: bool = True
    beam_size: int = 5


Progress = Callable[[float, str], None]  # fraction 0..1, message


# ------------------------------------------------------------------ helpers

def add_cuda_dll_dirs() -> None:
    """On Windows, make pip-installed CUDA libraries (requirements-gpu.txt) loadable."""
    if sys.platform != "win32":
        return
    for site in sys.path:
        for folder in glob.glob(os.path.join(site, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(folder)
                os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
            except OSError:
                pass


def cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 - any failure means "no GPU"
        return False


def resolve_device(requested: str) -> list[tuple[str, str]]:
    """(device, compute_type) pairs to try, best first."""
    cpu = ("cpu", "int8")
    if requested == "cpu":
        return [cpu]
    if requested == "cuda" or (requested == AUTO and cuda_available()):
        return [("cuda", "float16"), ("cuda", "int8_float16"), cpu]
    return [cpu]


def decode_audio(path: Path, ffmpeg: Path, audio_stream: int = 0, cancel: threading.Event | None = None) -> np.ndarray:
    """Decode an audio track to 16 kHz mono float32 samples."""
    cmd = [
        str(ffmpeg), "-v", "error", "-nostdin", "-i", str(path),
        "-map", f"0:a:{audio_stream}", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "f32le", "-",
    ]
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **no_window_kwargs())
    except OSError as exc:
        raise TranscriptionError(f"Could not start FFmpeg: {exc}") from exc
    chunks: list[bytes] = []
    assert process.stdout is not None
    while True:
        if cancel is not None and cancel.is_set():
            process.kill()
            process.wait()
            raise TranscriptionCancelled()
        data = process.stdout.read(1 << 20)
        if not data:
            break
        chunks.append(data)
    stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    if process.wait() != 0:
        message = stderr.strip().splitlines()[-1] if stderr.strip() else "unknown error"
        raise TranscriptionError(f"Could not read audio from {Path(path).name}: {message}")
    raw = b"".join(chunks)
    return np.frombuffer(raw[: len(raw) - len(raw) % 4], dtype="<f4").copy()


def convert_segments(raw_segments, offset_us: int = 0) -> tuple[Segment, ...]:
    """faster-whisper segments → our immutable model (works on any objects
    with the same attributes, which keeps it testable without a model)."""
    result: list[Segment] = []
    for index, seg in enumerate(raw_segments, start=1):
        start = offset_us + seconds_to_us(seg.start)
        end = max(start, offset_us + seconds_to_us(seg.end))
        words: list[Word] = []
        for w in getattr(seg, "words", None) or []:
            text = (w.word or "").strip()
            if not text:
                continue
            w_start = offset_us + seconds_to_us(w.start)
            w_end = max(w_start, offset_us + seconds_to_us(w.end))
            words.append(Word(w_start, w_end, text, float(getattr(w, "probability", 1.0)), WORD_TIMING))
        text = " ".join((seg.text or "").split())
        if not words and text:
            words = [Word(start, end, t, 0.0, SEGMENT_TIMING) for t in text.split(" ")]
        elif words:
            text = " ".join(w.text for w in words)
        if text:
            result.append(Segment(index, start, end, text, tuple(words)))
    return tuple(result)


# ------------------------------------------------------------------ engine

class WhisperEngine:
    """Loads a model once and reuses it for later clips."""

    def __init__(self, models_dir: Path) -> None:
        self.models_dir = Path(models_dir)
        self._model = None
        self._key: tuple[str, str, str] | None = None
        self.device_used = ""
        self._lock = threading.Lock()

    def _load(self, options: TranscribeOptions, progress: Progress) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise TranscriptionError("faster-whisper is not installed. Run run.bat again.") from exc
        add_cuda_dll_dirs()
        errors: list[str] = []
        for device, compute in resolve_device(options.device):
            key = (options.model, device, compute)
            if self._model is not None and self._key == key:
                return
            progress(0.0, f"Loading model '{options.model}' on {device.upper()} (first use downloads it)…")
            try:
                self.models_dir.mkdir(parents=True, exist_ok=True)
                self._model = WhisperModel(
                    options.model, device=device, compute_type=compute, download_root=str(self.models_dir)
                )
                self._key = key
                self.device_used = device
                log.info("Whisper model %s loaded on %s (%s)", options.model, device, compute)
                return
            except Exception as exc:  # noqa: BLE001 - try the next device
                log.warning("Could not load model on %s/%s: %s", device, compute, exc)
                errors.append(f"{device}: {exc}")
                self._model = None
        raise TranscriptionError("Could not load the speech model.\n" + "\n".join(errors))

    def transcribe(
        self,
        audio: np.ndarray,
        options: TranscribeOptions,
        progress: Progress = lambda f, m: None,
        cancel: threading.Event | None = None,
    ) -> Transcript:
        duration_s = len(audio) / SAMPLE_RATE
        with self._lock:
            self._load(options, progress)
            language = None if options.language == AUTO else options.language
            try:
                segments_iter, info = self._model.transcribe(
                    audio,
                    language=language,
                    task="transcribe",
                    beam_size=options.beam_size,
                    word_timestamps=True,
                    vad_filter=options.vad,
                    vad_parameters={"min_silence_duration_ms": 500},
                    condition_on_previous_text=False,
                )
                raw = []
                progress(0.02, f"Recognising ({info.language})…")
                for seg in segments_iter:  # the generator does the actual work
                    if cancel is not None and cancel.is_set():
                        raise TranscriptionCancelled()
                    raw.append(seg)
                    if duration_s > 0:
                        progress(min(0.99, seg.end / duration_s), f"Recognising ({info.language})… {len(raw)} segments")
            except TranscriptionCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                if self.device_used == "cuda" and options.device == AUTO:
                    # Typical cause: cuBLAS/cuDNN missing. Retry once on CPU.
                    log.warning("GPU transcription failed (%s); retrying on CPU", exc)
                    self._model = None
                    cpu = TranscribeOptions(options.model, options.language, "cpu", options.vad, options.beam_size)
                    self._lock.release()
                    try:
                        return self.transcribe(audio, cpu, progress, cancel)
                    finally:
                        self._lock.acquire()
                raise TranscriptionError(f"Transcription failed: {exc}") from exc
        progress(1.0, "Done")
        return Transcript(
            language=info.language,
            language_probability=float(info.language_probability),
            model=options.model,
            segments=convert_segments(raw),
            duration_us=seconds_to_us(duration_s),
        )


def transcribe_file(
    engine: WhisperEngine,
    path: Path,
    ffmpeg: Path,
    options: TranscribeOptions,
    progress: Progress = lambda f, m: None,
    cancel: threading.Event | None = None,
    audio_stream: int = 0,
) -> Transcript:
    progress(0.0, "Decoding audio…")
    audio = decode_audio(path, ffmpeg, audio_stream, cancel)
    if audio.size == 0:
        raise TranscriptionError("The audio track is empty.")
    return engine.transcribe(audio, options, progress, cancel)


def downloaded_models(models_dir: Path) -> list[str]:
    """Model sizes already present in ``models_dir`` (Hugging Face cache layout)."""
    found = []
    for size in MODEL_SIZES:
        pattern = os.path.join(str(models_dir), f"models--*faster-whisper-{size}", "snapshots", "*", "model.bin")
        if glob.glob(pattern):
            found.append(size)
    return found

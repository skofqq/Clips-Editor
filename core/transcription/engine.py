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
import re
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
    vocabulary: tuple[str, ...] = ()  # nicknames, slang: biases recognition towards them
    uncensored: bool = True  # prime the model to write swear words as spoken


Progress = Callable[[float, str], None]  # fraction 0..1, message


# ------------------------------------------------------------------ prompts

# Whisper tends to soften or skip swear words and to "correct" unusual
# nicknames into ordinary words. A short context prompt that already
# contains such words makes it far more likely to write them as spoken.
_PRIMING = {
    "ru": "Стрим на Twitch, живая речь без цензуры, с матом: блять, сука, нахуй, пиздец, ебать, хуйня.",
    "en": "Twitch stream, casual uncensored speech with swearing: fuck, fucking, shit, bitch, damn.",
}
_PLAIN = {"ru": "Стрим на Twitch, живая разговорная речь.", "en": "Twitch stream, casual speech."}
MAX_VOCABULARY_CHARS = 300  # Whisper keeps only ~220 prompt tokens


def parse_vocabulary(text: str) -> tuple[str, ...]:
    """Split a comma/newline separated list, dropping blanks and duplicates."""
    seen: dict[str, str] = {}
    for item in re.split(r"[,;\n]+", text or ""):
        item = " ".join(item.split())
        if item and item.casefold() not in seen:
            seen[item.casefold()] = item
    return tuple(seen.values())


def build_prompt(language: str, vocabulary: tuple[str, ...] = (), uncensored: bool = True) -> str | None:
    """Context prompt for Whisper, or None when there is nothing to add."""
    table = _PRIMING if uncensored else _PLAIN
    if language in table:
        parts = [table[language]]
    elif uncensored:  # auto-detect: prime both languages
        parts = [_PRIMING["ru"], _PRIMING["en"]]
    else:
        parts = []
    if vocabulary:
        names = ", ".join(vocabulary)
        if len(names) > MAX_VOCABULARY_CHARS:
            names = names[:MAX_VOCABULARY_CHARS].rsplit(",", 1)[0]
        label = "Ники и имена" if language == "ru" else "Names"
        parts.append(f"{label}: {names}.")
    return " ".join(parts) if parts else None


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


if sys.platform == "win32":
    CUDA_LIBRARIES = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll", "cudnn_ops64_9.dll", "cudnn_cnn64_9.dll")
else:
    CUDA_LIBRARIES = ("libcublas.so.12", "libcublasLt.so.12", "libcudnn.so.9", "libcudnn_ops.so.9", "libcudnn_cnn.so.9")

GPU_HELP = "Run install_gpu.bat in the application folder to add GPU support (about 1 GB)."


def missing_cuda_libraries() -> list[str]:
    """CUDA runtime libraries that cannot be loaded.

    CTranslate2 only loads cuBLAS/cuDNN when it first runs on the GPU, and a
    missing cuDNN can kill the whole process instead of raising, so this is
    checked up front.
    """
    import ctypes

    add_cuda_dll_dirs()
    loader = ctypes.WinDLL if sys.platform == "win32" else ctypes.CDLL  # type: ignore[attr-defined]
    missing = []
    for name in CUDA_LIBRARIES:
        try:
            loader(name)
        except OSError:
            missing.append(name)
    return missing


def resolve_device(requested: str) -> tuple[list[tuple[str, str]], str]:
    """(device, compute_type) pairs to try, best first, and a note for the
    user when the GPU cannot be used."""
    cpu = ("cpu", "int8")
    if requested == "cpu":
        return [cpu], ""
    if not cuda_available():
        note = "" if requested == AUTO else "No CUDA-capable GPU was found, so the CPU was used."
        return [cpu], note
    missing = missing_cuda_libraries()
    if missing:
        note = f"GPU libraries are not installed ({', '.join(missing[:2])}…), so the CPU was used. {GPU_HELP}"
        return [cpu], note
    return [("cuda", "float16"), ("cuda", "int8_float16"), cpu], ""


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
        self.notice = ""  # why the GPU was not used, if it was requested/available
        self._lock = threading.Lock()

    def _load(self, model: str, candidates: list[tuple[str, str]], progress: Progress) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise TranscriptionError("faster-whisper is not installed. Run run.bat again.") from exc
        errors: list[str] = []
        for device, compute in candidates:
            key = (model, device, compute)
            if self._model is not None and self._key == key:
                return
            progress(0.0, f"Loading model '{model}' on {device.upper()} (first use downloads it)…")
            try:
                self.models_dir.mkdir(parents=True, exist_ok=True)
                self._model = WhisperModel(model, device=device, compute_type=compute, download_root=str(self.models_dir))
                self._key = key
                self.device_used = device
                log.info("Whisper model %s loaded on %s (%s)", model, device, compute)
                return
            except Exception as exc:  # noqa: BLE001 - try the next device
                log.warning("Could not load model on %s/%s: %s", device, compute, exc)
                errors.append(f"{device}: {exc}")
                self._model = None
                self._key = None
        raise TranscriptionError("Could not load the speech model.\n" + "\n".join(errors))

    def _run(self, audio: np.ndarray, options: TranscribeOptions, progress: Progress, cancel):
        duration_s = len(audio) / SAMPLE_RATE
        language = None if options.language == AUTO else options.language
        prompt = build_prompt(options.language, options.vocabulary, options.uncensored)
        segments_iter, info = self._model.transcribe(
            audio,
            language=language,
            task="transcribe",
            beam_size=options.beam_size,
            word_timestamps=True,
            vad_filter=options.vad,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            # initial_prompt only conditions the first 30 s window; hotwords are
            # added to every window, so the context holds for the whole clip.
            initial_prompt=prompt,
            hotwords=prompt,
        )
        raw = []
        progress(0.02, f"Recognising ({info.language}) on {self.device_used.upper()}…")
        for seg in segments_iter:  # the generator does the actual work
            if cancel is not None and cancel.is_set():
                raise TranscriptionCancelled()
            raw.append(seg)
            if duration_s > 0:
                progress(min(0.99, seg.end / duration_s),
                         f"Recognising ({info.language}) on {self.device_used.upper()}… {len(raw)} segments")
        return raw, info

    def transcribe(
        self,
        audio: np.ndarray,
        options: TranscribeOptions,
        progress: Progress = lambda f, m: None,
        cancel: threading.Event | None = None,
    ) -> Transcript:
        with self._lock:
            candidates, self.notice = resolve_device(options.device)
            if self.notice:
                log.warning("%s", self.notice)
            self._load(options.model, candidates, progress)
            try:
                raw, info = self._run(audio, options, progress, cancel)
            except TranscriptionCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                if self.device_used != "cuda":
                    raise TranscriptionError(f"Transcription failed: {exc}") from exc
                # The GPU failed while running (e.g. a CUDA library is broken). Retry on CPU.
                log.warning("GPU transcription failed (%s); retrying on CPU", exc)
                self.notice = f"The GPU failed ({exc}), so the CPU was used. {GPU_HELP}"
                self._model = None
                self._key = None
                self._load(options.model, [("cpu", "int8")], progress)
                try:
                    raw, info = self._run(audio, options, progress, cancel)
                except TranscriptionCancelled:
                    raise
                except Exception as exc2:  # noqa: BLE001
                    raise TranscriptionError(f"Transcription failed: {exc2}") from exc2
        progress(1.0, "Done")
        return Transcript(
            language=info.language,
            language_probability=float(info.language_probability),
            model=options.model,
            segments=convert_segments(raw),
            duration_us=seconds_to_us(len(audio) / SAMPLE_RATE),
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

"""Speech recognition with non-Whisper models through onnx-asr (ONNX Runtime, no PyTorch).

* GigaAM v3 (Sber, Russian): a CTC model. It writes what it hears, swear
  words included, where Whisper tends to soften or drop them, and its
  per-letter timing gives precise word edges.
* Parakeet TDT 0.6B v3 (NVIDIA, 25 European languages incl. English and
  Russian): fast and accurate, especially on English.

The audio is cut into speech chunks of at most ~20 s (these models do not
take longer input) with the Silero VAD that faster-whisper ships, then each
chunk is recognised with token timestamps that are grouped into words.

Models are downloaded once into ``models_dir/onnx/<key>``.
"""

from __future__ import annotations

import logging
import math
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from core.timeline.timebase import seconds_to_us
from core.transcription.model import WORD_TIMING, Segment, Transcript, Word

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
MAX_CHUNK_S = 20.0
COMPLETE_MARK = ".complete"
Progress = Callable[[float, str], None]


@dataclass(frozen=True)
class OnnxModel:
    key: str  # the name shown in the app
    onnx_name: str  # onnx-asr model name
    languages: tuple[str, ...]  # () = many languages
    approx_mb: int
    frame_s: float  # time per output frame (token timing step)
    description: str


GIGAAM = "gigaam-v3"
PARAKEET = "parakeet-v3"
ONNX_MODELS = {
    GIGAAM: OnnxModel(GIGAAM, "gigaam-v3-e2e-ctc", ("ru",), 250, 0.04,
                      "Sber GigaAM v3: Russian only, writes swear words as spoken, precise word timing, fast on a CPU"),
    PARAKEET: OnnxModel(PARAKEET, "nemo-parakeet-tdt-0.6b-v3", (), 670, 0.08,
                        "NVIDIA Parakeet v3: English and 24 more languages incl. Russian, fast and accurate"),
}
QUANTIZATIONS = ("int8", None)  # smaller int8 files first, full precision if a repo has none


class OnnxError(Exception):
    pass


def is_onnx_model(name: str) -> bool:
    return name in ONNX_MODELS


def model_dir(models_dir: Path, key: str) -> Path:
    return Path(models_dir) / "onnx" / key


def is_downloaded(models_dir: Path, key: str) -> bool:
    return (model_dir(models_dir, key) / COMPLETE_MARK).is_file()


def downloaded(models_dir: Path) -> list[str]:
    return [key for key in ONNX_MODELS if is_downloaded(models_dir, key)]


def delete_model(models_dir: Path, key: str) -> bool:
    folder = model_dir(models_dir, key)
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()


def _folder_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def _load(model: OnnxModel, folder: Path, quantization: str | None):
    import onnx_asr

    return onnx_asr.load_model(model.onnx_name, folder, quantization=quantization,
                               providers=["CPUExecutionProvider"])


def load_model(models_dir: Path, key: str, progress: Progress | None = None,
               cancel: threading.Event | None = None):
    """The onnx-asr model ``key`` (with timestamps), downloading it on first use."""
    try:
        import onnx_asr  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise OnnxError("onnx-asr is not installed. Run run.bat again.") from exc
    model = ONNX_MODELS[key]
    folder = model_dir(models_dir, key)
    progress = progress or (lambda *_a: None)
    if is_downloaded(models_dir, key):
        quantization = (folder / COMPLETE_MARK).read_text(encoding="utf-8").strip() or None
        try:
            return _load(model, folder, quantization).with_timestamps()
        except Exception as exc:  # noqa: BLE001 - damaged: download again
            log.warning("Could not load %s from %s (%s); downloading again", key, folder, exc)
    shutil.rmtree(folder, ignore_errors=True)
    folder.parent.mkdir(parents=True, exist_ok=True)
    if cancel is not None and cancel.is_set():
        raise InterruptedError("cancelled")
    done = threading.Event()

    def watch() -> None:
        while not done.wait(0.5):
            have = _folder_size(folder) if folder.exists() else 0
            progress(min(0.99, have / (model.approx_mb * 1024 * 1024)),
                     f"Downloading {key}: {have // (1024 * 1024)} of ~{model.approx_mb} MB")

    progress(0.0, f"Downloading {key} (about {model.approx_mb} MB, once)…")
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    errors = []
    try:
        for quantization in QUANTIZATIONS:
            try:
                asr = _load(model, folder, quantization)
            except Exception as exc:  # noqa: BLE001 - try the next precision
                errors.append(f"{quantization or 'full'}: {exc}")
                log.info("%s %s not available: %s", key, quantization, exc)
                shutil.rmtree(folder, ignore_errors=True)
                continue
            (folder / COMPLETE_MARK).write_text(quantization or "", encoding="utf-8")
            log.info("Downloaded %s (%s) to %s", key, quantization or "full precision", folder)
            return asr.with_timestamps()
    finally:
        done.set()
        watcher.join(timeout=2)
    raise OnnxError(f"Could not download or load {key}:\n" + "\n".join(errors))


def download(models_dir: Path, key: str, progress: Progress | None = None) -> Path:
    load_model(models_dir, key, progress)
    return model_dir(models_dir, key)


# ------------------------------------------------------------------ chunks
def speech_chunks(audio: np.ndarray, use_vad: bool = True, threshold: float = 0.5,
                  max_s: float = MAX_CHUNK_S) -> list[tuple[int, int]]:
    """(start, end) sample ranges of at most ``max_s`` seconds to recognise."""
    total = len(audio)
    limit = int(max_s * SAMPLE_RATE)
    if total == 0:
        return []
    chunks: list[tuple[int, int]] = []
    if use_vad:
        try:
            from faster_whisper.vad import VadOptions, get_speech_timestamps

            options = VadOptions(threshold=threshold, min_silence_duration_ms=300, speech_pad_ms=250,
                                 max_speech_duration_s=max_s - 1)
            chunks = [(int(c["start"]), int(c["end"])) for c in get_speech_timestamps(audio, options)]
        except Exception as exc:  # noqa: BLE001 - no VAD: plain windows
            log.info("VAD not available for ONNX models (%s); using fixed windows", exc)
            use_vad = False
    if not use_vad:
        chunks = [(start, min(total, start + limit)) for start in range(0, total, limit)]
    result = []
    for start, end in chunks:  # VAD padding can still exceed the limit
        start, end = max(0, start), min(total, end)
        while end - start > limit:
            result.append((start, start + limit))
            start += limit
        if end - start >= SAMPLE_RATE // 10:
            result.append((start, end))
    return result


def tokens_to_words(tokens, timestamps, logprobs, offset_s: float, chunk_end_s: float,
                    frame_s: float) -> list[Word]:
    """Group sub-word tokens (a leading space starts a word) into timed words.

    A word starts at its first token and ends shortly after its last one,
    never after the next word starts or the chunk ends."""
    tokens = list(tokens or [])
    timestamps = list(timestamps or [])
    if not tokens or len(timestamps) != len(tokens):
        return []
    logprobs = list(logprobs or []) if logprobs is not None and len(logprobs) == len(tokens) else [0.0] * len(tokens)
    groups: list[list[int]] = []
    new_word = True
    for i, token in enumerate(tokens):
        if not token.strip():  # a bare word separator
            new_word = True
            continue
        if new_word or token.startswith(" "):
            groups.append([i])
            new_word = False
        else:
            groups[-1].append(i)
    words: list[Word] = []
    starts = [timestamps[g[0]] for g in groups]
    for n, group in enumerate(groups):
        text = "".join(tokens[i] for i in group).strip()
        start = offset_s + starts[n]
        tail = offset_s + timestamps[group[-1]] + frame_s + 0.12
        limit = offset_s + starts[n + 1] - 0.01 if n + 1 < len(groups) else chunk_end_s
        end = max(start + frame_s, min(tail, limit, chunk_end_s))
        prob = math.exp(sum(logprobs[i] for i in group) / len(group)) if group else 1.0
        words.append(Word(seconds_to_us(start), seconds_to_us(end), text, float(min(1.0, prob)), WORD_TIMING))
    return words


# ------------------------------------------------------------------ engine
class OnnxEngine:
    """Keeps the last loaded ONNX model for the next clip."""

    def __init__(self, models_dir: Path) -> None:
        self.models_dir = Path(models_dir)
        self._key = ""
        self._asr = None

    def _model(self, key: str, progress: Progress, cancel) -> object:
        if self._asr is None or self._key != key:
            self._asr = None
            progress(0.0, f"Loading {key} (first use downloads it)…")
            self._asr = load_model(self.models_dir, key, progress, cancel)
            self._key = key
        return self._asr

    def transcribe(self, audio: np.ndarray, key: str, language: str = "auto", use_vad: bool = True,
                   vad_threshold: float = 0.5, progress: Progress = lambda f, m: None,
                   cancel: threading.Event | None = None) -> Transcript:
        from core.transcription.engine import TranscriptionCancelled

        model = ONNX_MODELS[key]
        asr = self._model(key, progress, cancel)
        chunks = speech_chunks(audio, use_vad, vad_threshold)
        segments: list[Segment] = []
        for n, (start, end) in enumerate(chunks, start=1):
            if cancel is not None and cancel.is_set():
                raise TranscriptionCancelled()
            progress(n / max(1, len(chunks)) * 0.99, f"Recognising with {key} on CPU… {n} of {len(chunks)}")
            result = asr.recognize(audio[start:end], sample_rate=SAMPLE_RATE)
            words = tokens_to_words(result.tokens, result.timestamps, getattr(result, "logprobs", None),
                                    start / SAMPLE_RATE, end / SAMPLE_RATE, model.frame_s)
            if not words:
                continue
            segments.append(Segment(len(segments) + 1, words[0].start_us, words[-1].end_us,
                                    " ".join(w.text for w in words), tuple(words)))
        text = " ".join(s.text for s in segments)
        if model.languages:
            detected = model.languages[0]
        elif language in ("ru", "en"):
            detected = language
        else:
            cyrillic = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
            latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
            detected = "ru" if cyrillic > latin else "en"
        progress(1.0, "Done")
        return Transcript(language=detected, language_probability=1.0, model=key, segments=tuple(segments),
                          duration_us=seconds_to_us(len(audio) / SAMPLE_RATE))

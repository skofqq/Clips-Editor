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
    slang: bool = True  # prime the model with gaming / streaming slang
    clean_audio: bool = True  # band-pass + loudness levelling before recognition
    vad_threshold: float = 0.5  # Silero speech probability; lower catches quieter speech
    accurate: bool = False  # wider beam search: slower, a little more accurate
    separate_voice: bool = False  # isolate the voice from game audio / music first (slower)


VAD_LEVELS = {"normal": 0.5, "sensitive": 0.3}
# Speech lives in roughly 80 Hz – 8 kHz; game rumble and hiss outside it only
# confuse the model.
AUDIO_CLEANUP = (
    "highpass=f=70,lowpass=f=7800,"
    # Shouting: squash peaks so clipped, overloaded words become readable...
    "acompressor=threshold=-20dB:ratio=4:attack=5:release=150:makeup=2,"
    # ...and lift quiet speech under loud game audio.
    "dynaudnorm=f=250:g=15:m=12,"
    "alimiter=limit=0.9:level=disabled"
)
MAX_LEARNED_WORDS = 60


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
# Gaming and streaming slang that Whisper otherwise "corrects" into ordinary
# words. Kept short: Whisper only reads the last ~220 prompt tokens, so the
# user's own names go last and are never cut off by this list.
SLANG = {
    "ru": (
        "катка", "тиммейты", "рофл", "кринж", "имба", "изи", "гг", "донат", "сабы", "лут", "фармить",
        "тильт", "руинер", "кд", "респавн", "пушить", "рейд", "клатч", "ваншот", "хедшот", "бафф", "нерф",
        "лагает", "афк", "ачивка", "жиза", "модер", "нуб", "скилл", "агрится",
    ),
    "en": (
        "GG", "clutch", "poggers", "KEKW", "copium", "sus", "based", "cracked", "nerf", "one-tap",
        "headshot", "respawn", "smurf", "tryhard", "raid", "gifted subs", "AFK", "grind", "noob", "no cap",
    ),
}
MAX_VOCABULARY_CHARS = 300  # Whisper keeps only ~220 prompt tokens


def parse_vocabulary(text: str) -> tuple[str, ...]:
    """Split a comma/newline separated list, dropping blanks and duplicates."""
    seen: dict[str, str] = {}
    for item in re.split(r"[,;\n]+", text or ""):
        item = " ".join(item.split())
        if item and item.casefold() not in seen:
            seen[item.casefold()] = item
    return tuple(seen.values())


def build_prompt(
    language: str,
    vocabulary: tuple[str, ...] = (),
    uncensored: bool = True,
    slang: bool = False,
    slang_words: int | None = None,
) -> str | None:
    """Context prompt for Whisper, or None when there is nothing to add."""
    table = _PRIMING if uncensored else _PLAIN
    if language in table:
        parts = [table[language]]
    elif uncensored:  # auto-detect: prime both languages
        parts = [_PRIMING["ru"], _PRIMING["en"]]
    else:
        parts = []
    if slang:
        languages = [language] if language in SLANG else ["ru", "en"]
        for lang in languages:
            label = "Сленг" if lang == "ru" else "Slang"
            words = SLANG[lang] if slang_words is None else SLANG[lang][:slang_words]
            if words:
                parts.append(f"{label}: {', '.join(words)}.")
    if vocabulary:
        names = ", ".join(vocabulary)
        if len(names) > MAX_VOCABULARY_CHARS:
            names = names[:MAX_VOCABULARY_CHARS].rsplit(",", 1)[0]
        label = "Ники и имена" if language == "ru" else "Names"
        parts.append(f"{label}: {names}.")
    return " ".join(parts) if parts else None


# Whisper's decoder holds 448 positions in total: prompt + the text of a
# 30 s window. A long prompt overflows it ("No position encodings are
# defined for positions >= 448"), so the prompt is kept well below half.
PROMPT_TOKEN_BUDGET = 150


def fit_prompt(options: "TranscribeOptions", count_tokens: Callable[[str], int]) -> str | None:
    """The richest prompt that fits PROMPT_TOKEN_BUDGET.

    Slang is shortened first, then dropped, then the nickname list is cut
    from the end; the swear-word priming and the first names are kept.
    """
    def build(vocabulary, slang_words=None, slang=options.slang):
        return build_prompt(options.language, vocabulary, options.uncensored, slang, slang_words)

    vocabulary = tuple(options.vocabulary)
    if options.slang:
        longest = max(len(words) for words in SLANG.values())
        for limit in range(longest, 0, -5):
            prompt = build(vocabulary, limit)
            if prompt is None or count_tokens(prompt) <= PROMPT_TOKEN_BUDGET:
                return prompt
    while True:
        prompt = build(vocabulary, slang=False)
        if prompt is None or count_tokens(prompt) <= PROMPT_TOKEN_BUDGET or not vocabulary:
            return prompt
        vocabulary = vocabulary[:-1]


def _estimate_tokens(text: str) -> int:
    """Pessimistic token count when the real tokenizer is not available."""
    return len(text.encode("utf-8")) // 2 + 1


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


def decode_audio(
    path: Path,
    ffmpeg: Path,
    audio_stream: int = 0,
    cancel: threading.Event | None = None,
    clean: bool = False,
    start_us: int | None = None,
    end_us: int | None = None,
    rate: int = SAMPLE_RATE,
    channels: int = 1,
) -> np.ndarray:
    """Decode an audio track (or a part of it) to float32 samples (16 kHz mono
    by default; interleaved when ``channels`` > 1)."""
    cmd = [str(ffmpeg), "-v", "error", "-nostdin"]
    if start_us is not None:
        cmd += ["-ss", f"{start_us / 1e6:.6f}"]
    cmd += ["-i", str(path)]
    if end_us is not None:
        cmd += ["-t", f"{(end_us - (start_us or 0)) / 1e6:.6f}"]
    cmd += ["-map", f"0:a:{audio_stream}", "-vn", "-ac", str(channels)]
    if clean:
        cmd += ["-af", AUDIO_CLEANUP]
    cmd += ["-ar", str(rate), "-f", "f32le", "-"]
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


def to_recognition_audio(samples: np.ndarray, rate: int, ffmpeg: Path, clean: bool) -> np.ndarray:
    """(channels, n) float32 at ``rate`` → 16 kHz mono, cleaned like decode_audio."""
    channels = samples.shape[0] if samples.ndim == 2 else 1
    data = np.ascontiguousarray(samples.T if samples.ndim == 2 else samples, dtype="<f4").tobytes()
    cmd = [str(ffmpeg), "-v", "error", "-nostdin", "-f", "f32le", "-ar", str(rate), "-ac", str(channels),
           "-i", "-", "-ac", "1"]
    if clean:
        cmd += ["-af", AUDIO_CLEANUP]
    cmd += ["-ar", str(SAMPLE_RATE), "-f", "f32le", "-"]
    try:
        done = subprocess.run(cmd, input=data, capture_output=True, **no_window_kwargs())
    except OSError as exc:
        raise TranscriptionError(f"Could not start FFmpeg: {exc}") from exc
    if done.returncode != 0:
        lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise TranscriptionError(f"Could not convert the separated voice: {lines[-1] if lines else 'unknown error'}")
    raw = done.stdout
    return np.frombuffer(raw[: len(raw) - len(raw) % 4], dtype="<f4").copy()


def _recognition_input(engine: "WhisperEngine", path: Path, ffmpeg: Path, options: "TranscribeOptions",
                       progress: Progress, cancel, audio_stream: int,
                       start_us: int | None = None, end_us: int | None = None) -> np.ndarray:
    if not options.separate_voice:
        return decode_audio(path, ffmpeg, audio_stream, cancel, options.clean_audio, start_us, end_us)
    from core.audio.separation import SAMPLE_RATE as SEP_RATE

    stereo = decode_audio(path, ffmpeg, audio_stream, cancel, False, start_us, end_us, rate=SEP_RATE, channels=2)
    if stereo.size == 0:
        return stereo
    vocals = engine.separate_voice(stereo.reshape(-1, 2).T, progress, cancel)
    return to_recognition_audio(vocals, SEP_RATE, ffmpeg, options.clean_audio)


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
        self._separator = None

    def separate_voice(self, stereo: np.ndarray, progress: Progress = lambda f, m: None,
                       cancel: threading.Event | None = None) -> np.ndarray:
        """Keep only the voice of (2, n) 44.1 kHz audio; downloads the model once."""
        from core.audio import separation

        try:
            if self._separator is None:
                path = separation.download_model(self.models_dir, progress=progress, cancel=cancel)
                progress(0.0, "Loading the voice separation model…")
                self._separator = separation.VocalSeparator(path)
            return self._separator.separate(stereo, progress, cancel)
        except separation.SeparationCancelled as exc:
            raise TranscriptionCancelled() from exc
        except separation.SeparationError as exc:
            raise TranscriptionError(str(exc)) from exc

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

    def _token_counter(self) -> Callable[[str], int]:
        tokenizer = getattr(self._model, "hf_tokenizer", None)
        if tokenizer is None:
            return _estimate_tokens

        def count(text: str) -> int:
            try:
                return len(tokenizer.encode(" " + text, add_special_tokens=False).ids)
            except Exception:  # noqa: BLE001 - fall back to the estimate
                return _estimate_tokens(text)

        return count

    def _run(self, audio: np.ndarray, options: TranscribeOptions, progress: Progress, cancel):
        duration_s = len(audio) / SAMPLE_RATE
        language = None if options.language == AUTO else options.language
        prompt = fit_prompt(options, self._token_counter())
        segments_iter, info = self._model.transcribe(
            audio,
            language=language,
            task="transcribe",
            beam_size=max(options.beam_size, 8) if options.accurate else options.beam_size,
            best_of=5,
            patience=1.5 if options.accurate else 1.0,
            word_timestamps=True,
            vad_filter=options.vad,
            # speech_pad_ms keeps word edges that the detector would clip.
            vad_parameters={
                "threshold": options.vad_threshold,
                "min_silence_duration_ms": 500,
                "speech_pad_ms": 300,
            },
            condition_on_previous_text=False,
            # Skip text "heard" in long silent stretches (typical hallucinations).
            hallucination_silence_threshold=2.0,
            # hotwords are added to every 30 s window, so the context holds for
            # the whole clip. Passing the same text as initial_prompt too would
            # double it in the first window and overflow the decoder.
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
    audio = _recognition_input(engine, path, ffmpeg, options, progress, cancel, audio_stream)
    if audio.size == 0:
        raise TranscriptionError("The audio track is empty.")
    return engine.transcribe(audio, options, progress, cancel)


def transcribe_range(
    engine: WhisperEngine,
    path: Path,
    ffmpeg: Path,
    options: TranscribeOptions,
    start_us: int,
    end_us: int,
    progress: Progress = lambda f, m: None,
    cancel: threading.Event | None = None,
    audio_stream: int = 0,
) -> tuple[Segment, ...]:
    """Recognise only ``start_us``–``end_us``; segments come back in clip time."""
    progress(0.0, "Decoding audio…")
    audio = _recognition_input(engine, path, ffmpeg, options, progress, cancel, audio_stream, start_us, end_us)
    if audio.size == 0:
        raise TranscriptionError("No audio in the selected range.")
    part = engine.transcribe(audio, options, progress, cancel)
    return shift_segments(part.segments, start_us)


def shift_segments(segments, offset_us: int) -> tuple[Segment, ...]:
    from dataclasses import replace as _replace

    return tuple(
        _replace(
            s,
            start_us=s.start_us + offset_us,
            end_us=s.end_us + offset_us,
            words=tuple(_replace(w, start_us=w.start_us + offset_us, end_us=w.end_us + offset_us) for w in s.words),
        )
        for s in segments
    )


_WORD = re.compile(r"[\w@'’-]+", re.UNICODE)


def learned_words(old_text: str, new_text: str) -> list[str]:
    """Words the user typed in a correction that were not there before."""
    before = {w.casefold() for w in _WORD.findall(old_text)}
    found: list[str] = []
    for word in _WORD.findall(new_text):
        word = word.strip("'’-")
        if len(word) < 3 or word.isdigit() or word.casefold() in before:
            continue
        if word.casefold() not in {f.casefold() for f in found}:
            found.append(word)
    return found


def merge_learned(learned: list[str], new: list[str], limit: int = MAX_LEARNED_WORDS) -> list[str]:
    """Most recent first, without duplicates, capped."""
    result: list[str] = []
    for word in list(new) + list(learned):
        if word.casefold() not in {r.casefold() for r in result}:
            result.append(word)
    return result[:limit]


def is_custom_model(model: str) -> bool:
    return model not in MODEL_SIZES


def downloaded_models(models_dir: Path) -> list[str]:
    """Model sizes already present in ``models_dir`` (Hugging Face cache layout)."""
    found = []
    for size in MODEL_SIZES:
        pattern = os.path.join(str(models_dir), f"models--*faster-whisper-{size}", "snapshots", "*", "model.bin")
        if glob.glob(pattern):
            found.append(size)
    return found

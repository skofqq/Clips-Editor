"""Who is speaking: label every transcript word with a speaker.

A speaker-embedding model (WeSpeaker CAM++, ONNX, ~28 MB, downloaded once)
turns 1.5 s windows of speech into voice "fingerprints". Windows are
grouped by agglomerative clustering on cosine similarity, either into a
given number of speakers or until the voices stop being similar enough.
Each word takes the speaker of the windows around it; a lone word between
two words of the same speaker is smoothed over.

Features are Kaldi-style log mel filterbanks (80 bins, 25 ms / 10 ms),
computed with numpy, so no PyTorch is needed.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from core.transcription.engine import TranscriptionError
from core.transcription.model import Transcript

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]
RATE = 16000
WINDOW_S = 1.5
HOP_S = 0.75
MIN_SPEECH_S = 0.4  # a window needs this much speech to count
SIMILARITY = 0.3  # merge clusters while their voices are at least this similar (auto mode)
MAX_SPEAKERS = 6


class DiarizationError(TranscriptionError):
    pass


class DiarizationCancelled(Exception):
    pass


@dataclass(frozen=True)
class SpeakerModel:
    file: str
    url: str
    size_mb: int


SPEAKER_MODEL = SpeakerModel(
    "wespeaker_en_voxceleb_CAM++.onnx",
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "wespeaker_en_voxceleb_CAM++.onnx",
    28,
)


def model_path(models_dir: Path, model: SpeakerModel = SPEAKER_MODEL) -> Path:
    return Path(models_dir) / "speakers" / model.file


def download_model(models_dir: Path, model: SpeakerModel = SPEAKER_MODEL, progress: Progress | None = None,
                   cancel: threading.Event | None = None) -> Path:
    from core.audio.download import DownloadCancelled, DownloadError, fetch

    try:
        return fetch(model.url, model_path(models_dir, model), model.size_mb, "the speaker model", progress, cancel)
    except DownloadCancelled as exc:
        raise DiarizationCancelled() from exc
    except DownloadError as exc:
        raise DiarizationError(str(exc)) from exc


# ------------------------------------------------------------------ features
def _mel_banks(bins: int, n_fft: int, rate: int, low: float = 20.0) -> np.ndarray:
    def mel(f):
        return 1127.0 * np.log(1.0 + np.asarray(f) / 700.0)

    high = rate / 2
    mel_points = np.linspace(mel(low), mel(high), bins + 2)
    fft_mel = mel(np.arange(n_fft // 2 + 1) * rate / n_fft)
    banks = np.zeros((bins, n_fft // 2 + 1), dtype=np.float32)
    for b in range(bins):
        left, centre, right = mel_points[b], mel_points[b + 1], mel_points[b + 2]
        up = (fft_mel - left) / (centre - left)
        down = (right - fft_mel) / (right - centre)
        banks[b] = np.maximum(0.0, np.minimum(up, down))
    return banks


_BANKS = _mel_banks(80, 512, RATE)


def fbank(samples: np.ndarray) -> np.ndarray:
    """Kaldi-compatible 80-bin log mel filterbank (frames, 80), mean-normalised."""
    x = samples.astype(np.float64) * 32768.0  # the model expects int16-scaled samples
    length, shift = 400, 160
    if x.size < length:
        return np.zeros((0, 80), dtype=np.float32)
    frames = 1 + (x.size - length) // shift
    idx = np.arange(length)[None, :] + shift * np.arange(frames)[:, None]
    f = x[idx]
    f = f - f.mean(axis=1, keepdims=True)  # remove DC
    f = np.concatenate([f[:, :1] * (1 - 0.97), f[:, 1:] - 0.97 * f[:, :-1]], axis=1)  # pre-emphasis
    f = f * np.power(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(length) / (length - 1)), 0.85)  # povey
    power = np.abs(np.fft.rfft(f, n=512, axis=1)) ** 2
    feats = np.log(np.maximum(power @ _BANKS.T, np.finfo(np.float32).eps))
    feats -= feats.mean(axis=0, keepdims=True)
    return feats.astype(np.float32)


class SpeakerEmbedder:
    def __init__(self, path: Path) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise DiarizationError("Finding speakers needs ONNX Runtime. Run run.bat again.") from exc
        options = ort.SessionOptions()
        options.log_severity_level = 3
        try:
            self.session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        except Exception as exc:  # noqa: BLE001
            raise DiarizationError(f"Could not load the speaker model: {exc}") from exc
        self.input_name = self.session.get_inputs()[0].name

    def embed(self, samples: np.ndarray) -> np.ndarray:
        feats = fbank(samples)
        vec = self.session.run(None, {self.input_name: feats[None]})[0][0]
        return vec / (np.linalg.norm(vec) + 1e-9)


# ------------------------------------------------------------------ clustering
def cluster(embeddings: np.ndarray, speakers: int | None = None, similarity: float | None = None) -> list[int]:
    """Average-linkage agglomerative clustering on cosine similarity."""
    similarity = SIMILARITY if similarity is None else similarity
    n = len(embeddings)
    if n == 0:
        return []
    groups: list[list[int]] = [[i] for i in range(n)]
    sim = embeddings @ embeddings.T
    target = max(1, min(speakers, n)) if speakers else 1
    while len(groups) > target:
        best, pair = -2.0, (0, 1)
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                value = float(sim[np.ix_(groups[a], groups[b])].mean())
                if value > best:
                    best, pair = value, (a, b)
        if speakers is None and best < similarity:
            break
        a, b = pair
        groups[a] += groups.pop(b)
    if speakers is None and len(groups) > MAX_SPEAKERS:
        return cluster(embeddings, MAX_SPEAKERS)
    # Number speakers by first appearance.
    labels = [0] * n
    order = sorted(groups, key=min)
    for label, members in enumerate(order):
        for i in members:
            labels[i] = label
    return labels


def _smooth(labels: list[int]) -> list[int]:
    out = list(labels)
    for i in range(1, len(out) - 1):
        if out[i - 1] == out[i + 1] != out[i]:
            out[i] = out[i - 1]
    return out


def find_speakers(transcript: Transcript, audio: np.ndarray, embedder: SpeakerEmbedder,
                  speakers: int | None = None, offset_us: int = 0, progress: Progress | None = None,
                  cancel: threading.Event | None = None, voices: dict | None = None) -> list[int]:
    """Speaker per word of ``transcript`` (order of ``transcript.words()``).

    ``audio`` is 16 kHz mono starting at ``offset_us`` of source time. A ``voices``
    dict is filled with each speaker's voice: speaker → (unit vector, windows heard).
    """
    progress = progress or (lambda f, m: None)
    words = transcript.words()
    if not words:
        return []
    spans = [((w.start_us - offset_us) / 1e6, (w.end_us - offset_us) / 1e6) for w in words]
    first, last = max(0.0, spans[0][0]), min(audio.size / RATE, max(e for _, e in spans))
    windows: list[tuple[float, float]] = []
    t = first
    while t < last:
        start = min(t, max(first, last - WINDOW_S))
        end = min(start + WINDOW_S, audio.size / RATE)
        speech = sum(max(0.0, min(end, e) - max(start, s)) for s, e in spans)
        if speech >= MIN_SPEECH_S and (not windows or windows[-1] != (start, end)):
            windows.append((start, end))
        t += HOP_S
    if not windows:
        return [0 if speakers else -1] * len(words)
    embeddings = []
    for k, (start, end) in enumerate(windows):
        if cancel is not None and cancel.is_set():
            raise DiarizationCancelled()
        embeddings.append(embedder.embed(audio[int(start * RATE):int(end * RATE)]))
        progress((k + 1) / len(windows), f"Listening for voices… {k + 1}/{len(windows)}")
    labels = cluster(np.stack(embeddings), speakers)
    if voices is not None:
        voices.update(speaker_voices(np.stack(embeddings), labels))
    centres = np.array([(s + e) / 2 for s, e in windows])
    result = []
    for s, e in spans:
        overlap = [(min(e, we) - max(s, ws), labels[i]) for i, (ws, we) in enumerate(windows) if min(e, we) > max(s, ws)]
        if overlap:
            votes: dict[int, float] = {}
            for amount, label in overlap:
                votes[label] = votes.get(label, 0.0) + amount
            result.append(max(votes, key=votes.get))
        else:
            result.append(labels[int(np.argmin(np.abs(centres - (s + e) / 2)))])
    return _smooth(result)


def speaker_voices(embeddings: np.ndarray, labels: list[int]) -> dict[int, tuple[np.ndarray, int]]:
    """Each speaker's average voice (unit vector) and how many windows it was heard in."""
    result = {}
    for label in sorted(set(labels)):
        rows = embeddings[[i for i, x in enumerate(labels) if x == label]]
        mean = rows.mean(axis=0)
        result[int(label)] = (mean / (np.linalg.norm(mean) + 1e-9), len(rows))
    return result


class SpeakerLabels(list):
    """Speaker per word, plus ``voices``: speaker → (voice vector, windows heard)."""

    def __init__(self, labels, voices=None) -> None:
        super().__init__(labels)
        self.voices = dict(voices or {})


_EMBEDDERS: dict[str, SpeakerEmbedder] = {}


def find_speakers_in_file(models_dir: Path, path: Path, ffmpeg: Path, transcript: Transcript,
                          speakers: int | None = None, progress: Progress | None = None,
                          cancel: threading.Event | None = None, audio_stream: int = 0) -> SpeakerLabels:
    """Decode the clip's audio and label every word of ``transcript`` with a speaker."""
    from core.transcription.engine import TranscriptionCancelled, decode_audio

    progress = progress or (lambda f, m: None)
    model = download_model(models_dir, progress=progress, cancel=cancel)
    embedder = _EMBEDDERS.get(str(model))
    if embedder is None:
        embedder = _EMBEDDERS[str(model)] = SpeakerEmbedder(model)
    progress(0.0, "Decoding audio…")
    try:
        audio = decode_audio(path, ffmpeg, audio_stream, cancel)
    except TranscriptionCancelled as exc:
        raise DiarizationCancelled() from exc
    voices: dict = {}
    labels = find_speakers(transcript, audio, embedder, speakers, 0, progress, cancel, voices)
    return SpeakerLabels(labels, voices)

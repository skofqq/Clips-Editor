"""Vocal separation: keep the streamer's voice, drop game audio and music.

Uses an MDX-Net vocal model (UVR "Kim Vocal 2", ONNX, ~65 MB) run with
ONNX Runtime (already installed with faster-whisper). The spectrogram is
computed with numpy, so no PyTorch is needed. It runs on the CPU (faster
than real time on a modern CPU); a CUDA or DirectML build of ONNX Runtime,
if installed, is used instead.

The model is downloaded once into the app's models folder on first use.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]
SAMPLE_RATE = 44100


class SeparationError(RuntimeError):
    pass


class SeparationCancelled(Exception):
    pass


@dataclass(frozen=True)
class MdxModel:
    file: str
    url: str
    size_mb: int
    n_fft: int = 7680
    hop: int = 1024
    dim_f: int = 3072
    dim_t: int = 256
    compensate: float = 1.009


VOCAL_MODEL = MdxModel(
    file="Kim_Vocal_2.onnx",
    url="https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/Kim_Vocal_2.onnx",
    size_mb=64,
)


def model_path(models_dir: Path, model: MdxModel = VOCAL_MODEL) -> Path:
    return Path(models_dir) / "separation" / model.file


def is_downloaded(models_dir: Path, model: MdxModel = VOCAL_MODEL) -> bool:
    path = model_path(models_dir, model)
    return path.is_file() and path.stat().st_size > 1_000_000


def download_model(models_dir: Path, model: MdxModel = VOCAL_MODEL, progress: Progress | None = None,
                   cancel: threading.Event | None = None) -> Path:
    from core.audio.download import DownloadCancelled, DownloadError, fetch

    if is_downloaded(models_dir, model):
        return model_path(models_dir, model)
    try:
        return fetch(model.url, model_path(models_dir, model), model.size_mb, "the voice model", progress, cancel)
    except DownloadCancelled as exc:
        raise SeparationCancelled() from exc
    except DownloadError as exc:
        raise SeparationError(f"Could not download the voice separation model: {exc}") from exc


# ------------------------------------------------------------------ STFT
def _window(n_fft: int) -> np.ndarray:
    return (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n_fft) / n_fft)).astype(np.float32)  # periodic Hann


def stft(x: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """``x`` (..., n) → complex (..., n_fft // 2 + 1, frames); centred like torch.stft."""
    pad = n_fft // 2
    padded = np.pad(x, [(0, 0)] * (x.ndim - 1) + [(pad, pad)], mode="reflect")
    frames = 1 + (padded.shape[-1] - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(frames)[:, None]
    windowed = padded[..., idx] * _window(n_fft)
    return np.swapaxes(np.fft.rfft(windowed, axis=-1), -1, -2)


def istft(spec: np.ndarray, n_fft: int, hop: int, length: int) -> np.ndarray:
    """Inverse of :func:`stft` (overlap-add with window normalisation)."""
    frames = spec.shape[-1]
    window = _window(n_fft)
    chunks = np.fft.irfft(np.swapaxes(spec, -1, -2), n=n_fft, axis=-1) * window
    total = n_fft + hop * (frames - 1)
    out = np.zeros(spec.shape[:-2] + (total,), dtype=np.float32)
    norm = np.zeros(total, dtype=np.float32)
    for f in range(frames):
        start = f * hop
        out[..., start:start + n_fft] += chunks[..., f, :]
        norm[start:start + n_fft] += window ** 2
    out /= np.maximum(norm, 1e-8)
    pad = n_fft // 2
    return out[..., pad:pad + length]


# ------------------------------------------------------------------ model
def _providers() -> list[str]:
    import onnxruntime as ort

    available = ort.get_available_providers()
    preferred = [p for p in ("CUDAExecutionProvider", "DmlExecutionProvider") if p in available]
    return preferred + ["CPUExecutionProvider"]


class VocalSeparator:
    def __init__(self, path: Path, model: MdxModel = VOCAL_MODEL, use_gpu: bool = True) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise SeparationError("Voice separation needs ONNX Runtime. Run run.bat again to install it.") from exc
        self.model = model
        options = ort.SessionOptions()
        options.log_severity_level = 3
        providers = _providers() if use_gpu else ["CPUExecutionProvider"]
        try:
            self.session = ort.InferenceSession(str(path), options, providers=providers)
        except Exception as exc:  # noqa: BLE001 - a broken GPU provider: fall back to the CPU
            if providers == ["CPUExecutionProvider"]:
                raise SeparationError(f"Could not load the voice separation model: {exc}") from exc
            log.warning("GPU separation unavailable (%s); using the CPU", exc)
            self.session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self.device = "GPU" if self.session.get_providers()[0] != "CPUExecutionProvider" else "CPU"
        self.input_name = self.session.get_inputs()[0].name

    def separate(self, audio: np.ndarray, progress: Progress | None = None,
                 cancel: threading.Event | None = None) -> np.ndarray:
        """``audio``: (2, n) float32 at 44.1 kHz → the vocals, same shape."""
        m = self.model
        progress = progress or (lambda f, msg: None)
        if audio.ndim == 1:
            audio = np.stack([audio, audio])
        n = audio.shape[-1]
        chunk = m.hop * (m.dim_t - 1)
        trim = m.n_fft // 2
        gen = chunk - 2 * trim
        pad = gen - n % gen
        mix = np.concatenate([np.zeros((2, trim), np.float32), audio.astype(np.float32),
                              np.zeros((2, pad + trim), np.float32)], axis=1)
        bins = m.n_fft // 2 + 1
        steps = list(range(0, n + pad, gen))
        out = []
        for k, start in enumerate(steps):
            if cancel is not None and cancel.is_set():
                raise SeparationCancelled()
            piece = mix[:, start:start + chunk]
            spec = stft(piece, m.n_fft, m.hop)[:, : m.dim_f]  # (2, F, T)
            x = np.stack([spec[0].real, spec[0].imag, spec[1].real, spec[1].imag])[None].astype(np.float32)
            y = self.session.run(None, {self.input_name: x})[0][0]
            full = np.zeros((2, bins, m.dim_t), np.complex64)
            full[0, : m.dim_f] = y[0] + 1j * y[1]
            full[1, : m.dim_f] = y[2] + 1j * y[3]
            wave = istft(full, m.n_fft, m.hop, chunk)
            out.append(wave[:, trim:-trim])
            progress((k + 1) / len(steps), f"Separating the voice ({self.device})… {k + 1}/{len(steps)}")
        vocals = np.concatenate(out, axis=1)[:, :n] * m.compensate
        return vocals.astype(np.float32)

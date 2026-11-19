"""Voice separation: STFT, the MDX chunking, model download, the pipeline."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from core.audio import separation
from core.audio.separation import MdxModel, VocalSeparator, istft, stft
from core.transcription import engine as engine_mod
from core.transcription.model import Transcript
from core.transcription.engine import SAMPLE_RATE, TranscribeOptions, TranscriptionCancelled, WhisperEngine


class _Identity:
    """Stands in for the ONNX session: returns the spectrogram unchanged."""

    def run(self, _outputs, feeds):
        return [next(iter(feeds.values()))]


def _separator(model: MdxModel = separation.VOCAL_MODEL) -> VocalSeparator:
    sep = VocalSeparator.__new__(VocalSeparator)
    sep.model, sep.session, sep.device, sep.input_name = model, _Identity(), "CPU", "input"
    return sep


def test_stft_round_trip():
    x = np.random.default_rng(0).standard_normal((2, 30_000)).astype(np.float32)
    spec = stft(x, 2048, 512)
    assert spec.shape == (2, 1025, 1 + 30_000 // 512)
    assert np.abs(istft(spec, 2048, 512, 30_000) - x).max() < 1e-4


def test_identity_model_reconstructs_the_audio():
    # Small model geometry keeps the test fast; the chunking logic is the same.
    model = MdxModel("x.onnx", "", 0, n_fft=1024, hop=256, dim_f=512, dim_t=64, compensate=1.0)
    t = np.arange(50_000) / separation.SAMPLE_RATE
    audio = np.stack([np.sin(2 * np.pi * 440 * t), 0.5 * np.sin(2 * np.pi * 300 * t)]).astype(np.float32)
    steps = []
    out = _separator(model).separate(audio, lambda f, m: steps.append(f))
    assert out.shape == audio.shape
    assert np.abs(out - audio).max() < 1e-3  # below dim_f nothing is lost
    assert steps[-1] == 1.0 and len(steps) > 1
    mono = _separator(model).separate(audio[0])
    assert mono.shape == (2, audio.shape[1])

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(separation.SeparationCancelled):
        _separator(model).separate(audio, cancel=cancel)


def test_download_model(tmp_path, monkeypatch):
    payload = b"m" * 2_000_000

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            if self.path != "/model.onnx":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        model = MdxModel("model.onnx", base + "/model.onnx", 2)
        messages = []
        path = separation.download_model(tmp_path, model, lambda f, m: messages.append(m))
        assert path.read_bytes() == payload and separation.is_downloaded(tmp_path, model)
        assert "Downloading the voice model" in messages[-1]
        assert not list(path.parent.glob("*.part"))
        missing = MdxModel("other.onnx", base + "/nope.onnx", 2)
        with pytest.raises(separation.SeparationError):
            separation.download_model(tmp_path, missing)
        assert not separation.is_downloaded(tmp_path, missing)
    finally:
        server.shutdown()
        server.server_close()


def test_transcribe_file_separates_the_voice_first(sample_clips, ffmpeg_paths, tmp_path, monkeypatch):
    engine = WhisperEngine(tmp_path)
    seen = {}

    def fake_separate(stereo, progress=None, cancel=None):
        seen["shape"] = stereo.shape
        return stereo * 0.5

    def fake_transcribe(audio, options, progress=None, cancel=None):
        seen["audio"] = audio
        return Transcript("ru", 1.0, "small", (), 0)

    monkeypatch.setattr(engine, "separate_voice", fake_separate)
    monkeypatch.setattr(engine, "transcribe", fake_transcribe)
    plain_len = len(engine_mod.decode_audio(sample_clips["mp4"], ffmpeg_paths.ffmpeg))
    assert engine_mod.transcribe_file(engine, sample_clips["mp4"], ffmpeg_paths.ffmpeg,
                                      TranscribeOptions(separate_voice=True, clean_audio=False)).language == "ru"
    assert seen["shape"][0] == 2 and seen["shape"][1] == pytest.approx(2 * separation.SAMPLE_RATE, rel=0.05)
    assert len(seen["audio"]) == pytest.approx(plain_len, rel=0.02)  # back to 16 kHz mono

    seen.clear()
    engine_mod.transcribe_range(engine, sample_clips["mp4"], ffmpeg_paths.ffmpeg,
                                TranscribeOptions(separate_voice=True), 500_000, 1_500_000)
    assert seen["shape"][1] == pytest.approx(separation.SAMPLE_RATE, rel=0.05)
    assert len(seen["audio"]) == pytest.approx(SAMPLE_RATE, rel=0.05)


def test_engine_separation_errors_map_to_transcription_errors(tmp_path, monkeypatch):
    engine = WhisperEngine(tmp_path)

    def cancelled(*a, **k):
        raise separation.SeparationCancelled()

    monkeypatch.setattr(separation, "download_model", cancelled)
    with pytest.raises(TranscriptionCancelled):
        engine.separate_voice(np.zeros((2, 10), np.float32))

    def offline(*a, **k):
        raise separation.SeparationError("Could not download")

    monkeypatch.setattr(separation, "download_model", offline)
    with pytest.raises(engine_mod.TranscriptionError, match="Could not download"):
        engine.separate_voice(np.zeros((2, 10), np.float32))


def test_panel_and_config_keep_the_option(qapp, data_dir):
    from app.config import AppConfig
    from ui.transcript.transcript_controller import TranscriptController
    from ui.transcript.transcript_panel import TranscriptPanel

    panel = TranscriptPanel(TranscriptController())
    assert not panel.options().separate_voice
    panel.set_quality_preferences(True, "normal", False, "", separate=True)
    assert panel.options().separate_voice and panel.quality_preferences()["separate"]
    config = AppConfig.load(data_dir)
    config.transcribe_separate_voice = True
    config.save()
    assert AppConfig.load(data_dir).transcribe_separate_voice
    panel.deleteLater()


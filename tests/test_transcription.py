"""Transcript model, engine glue (without a real model), panel and window."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.config import AppConfig
from core.projects.project import Project, ProjectFormatError, load_project, save_project
from core.transcription import engine as engine_mod
from core.transcription.engine import (
    SAMPLE_RATE,
    TranscribeOptions,
    TranscriptionCancelled,
    TranscriptionError,
    WhisperEngine,
    convert_segments,
    decode_audio,
    downloaded_models,
    resolve_device,
    transcribe_file,
)
from core.transcription.model import SEGMENT_TIMING, WORD_TIMING, Segment, Transcript, Word
from ui.transcript.transcript_controller import TranscriptController
from ui.transcript.transcript_panel import ROLE_KIND, TranscriptPanel

S = 1_000_000


def raw_seg(start, end, text, words=None):
    ws = [SimpleNamespace(start=a, end=b, word=w, probability=p) for a, b, w, p in (words or [])]
    return SimpleNamespace(start=start, end=end, text=text, words=ws)


def sample_transcript() -> Transcript:
    segments = convert_segments([
        raw_seg(2.12, 3.9, " Привет, ребята!", [(2.12, 2.6, " Привет,", 0.98), (2.7, 3.9, " ребята!", 0.95)]),
        raw_seg(4.5, 7.0, " Сейчас покажу интересный момент."),  # no word timing
        raw_seg(7.23, 8.8, " That was unexpected!", [(7.23, 7.5, " That", 0.9), (7.5, 7.8, " was", 0.9),
                                                    (7.8, 8.8, " unexpected!", 0.8)]),
        raw_seg(9.0, 9.5, "   "),  # empty: dropped
    ])
    return Transcript("ru", 0.97, "small", segments, 10 * S)


# ------------------------------------------------------------------ model

def test_convert_segments_keeps_word_timing_and_marks_missing():
    t = sample_transcript()
    assert [s.id for s in t.segments] == [1, 2, 3]
    first = t.segments[0]
    assert first.text == "Привет, ребята!"
    assert first.words[0] == Word(2_120_000, 2_600_000, "Привет,", 0.98, WORD_TIMING)
    assert first.has_word_timing
    second = t.segments[1]
    assert not second.has_word_timing
    assert all(w.timing == SEGMENT_TIMING and (w.start_us, w.end_us) == (4_500_000, 7 * S) for w in second.words)
    assert t.exact_word_ratio == pytest.approx(5 / 9)
    assert t.segment_at(5 * S).id == 2 and t.segment_at(4 * S) is None
    assert t.word_at(7_600_000).text == "was"
    assert t.word_at(5 * S) is None  # approximate words are never reported as exact hits


def test_editing_keeps_timing_when_word_count_matches():
    t = sample_transcript()
    fixed = t.with_segment_text(1, "Привет,  ребята!!")
    seg = fixed.segment(1)
    assert seg.edited and seg.text == "Привет, ребята!!"
    assert seg.has_word_timing and seg.words[1].text == "ребята!!"
    assert seg.words[1].start_us == 2_700_000

    rewritten = t.with_segment_text(3, "It was totally unexpected")
    seg = rewritten.segment(3)
    assert not seg.has_word_timing
    assert [w.timing for w in seg.words] == [SEGMENT_TIMING] * 4
    assert all((w.start_us, w.end_us) == (seg.start_us, seg.end_us) for w in seg.words)

    word_fix = t.with_word_text(3, 2, "unexpected?")
    assert word_fix.segment(3).text == "That was unexpected?"
    assert word_fix.segment(3).words[2].exact
    assert t.with_word_text(3, 2, "two words") is t
    assert t.with_word_text(3, 9, "x") is t
    assert t.with_segment_text(1, "Привет, ребята!") is t
    assert t.with_segment_text(99, "x") is t


def test_transcript_roundtrip_and_project(tmp_path):
    t = sample_transcript().with_segment_text(2, "Сейчас покажу момент")
    assert Transcript.from_dict(t.to_dict()) == t
    for bad in ({"segments": [{"id": 1, "start_us": 5, "end_us": 1}]},
                {"segments": [{"id": 1, "start_us": 0, "end_us": 1, "words": [[0, 1, "a", 1.0, "guess"]]}]},
                "x"):
        with pytest.raises((ValueError, KeyError, TypeError)):
            Transcript.from_dict(bad)

    project = Project()
    project.set_transcript(r"C:\a.mp4", t)
    loaded = load_project(save_project(project, tmp_path / "p.tssproj"))
    assert loaded.transcript_for(r"c:\A.mp4") == t
    loaded.set_transcript(r"C:\a.mp4", None)
    assert not loaded.transcripts
    (tmp_path / "bad.tssproj").write_text('{"schema_version": 1, "transcripts": [{"media": "a", "segments": 5}]}')
    with pytest.raises(ProjectFormatError):
        load_project(tmp_path / "bad.tssproj")


# ----------------------------------------------------------------- engine

class FakeModel:
    """Stands in for faster_whisper.WhisperModel."""

    calls: list = []
    fail_on: set = set()

    def __init__(self, size, device, compute_type, download_root):
        if device in self.fail_on:
            raise RuntimeError(f"no {device}")
        FakeModel.calls.append((size, device, compute_type))
        self.device = device

    def transcribe(self, audio, language=None, **kwargs):
        assert kwargs["word_timestamps"] and kwargs["vad_filter"]
        info = SimpleNamespace(language=language or "en", language_probability=0.9)
        duration = len(audio) / SAMPLE_RATE

        def gen():
            if "runtime" in self.fail_on:
                raise RuntimeError("cublas64_12.dll not found")
            yield raw_seg(0.1, duration / 2, "hello there", [(0.1, 0.4, "hello", 0.9), (0.5, 0.9, "there", 0.9)])
            yield raw_seg(duration / 2, duration, "second half")

        return gen(), info


@pytest.fixture
def fake_whisper(monkeypatch):
    import faster_whisper

    FakeModel.calls = []
    FakeModel.fail_on = set()
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    return FakeModel


def test_resolve_device(monkeypatch):
    monkeypatch.setattr(engine_mod, "cuda_available", lambda: False)
    assert resolve_device("auto") == ([("cpu", "int8")], "")
    devices, note = resolve_device("cuda")
    assert devices == [("cpu", "int8")] and "No CUDA" in note
    monkeypatch.setattr(engine_mod, "cuda_available", lambda: True)
    monkeypatch.setattr(engine_mod, "missing_cuda_libraries", lambda: [])
    devices, note = resolve_device("auto")
    assert devices[0] == ("cuda", "float16") and devices[-1] == ("cpu", "int8") and note == ""
    assert resolve_device("cpu") == ([("cpu", "int8")], "")
    # GPU present but cuBLAS/cuDNN missing (the usual Windows case): CPU, with advice.
    monkeypatch.setattr(engine_mod, "missing_cuda_libraries", lambda: ["cublas64_12.dll", "cudnn64_9.dll"])
    for requested in ("auto", "cuda"):
        devices, note = resolve_device(requested)
        assert devices == [("cpu", "int8")]
        assert "cublas64_12.dll" in note and "install_gpu.bat" in note


def test_missing_cuda_libraries_returns_names():
    missing = engine_mod.missing_cuda_libraries()
    assert set(missing) <= set(engine_mod.CUDA_LIBRARIES)


def test_engine_transcribes_and_reuses_model(fake_whisper, tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "cuda_available", lambda: False)
    engine = WhisperEngine(tmp_path)
    audio = np.zeros(SAMPLE_RATE * 4, dtype=np.float32)
    steps = []
    t = engine.transcribe(audio, TranscribeOptions(model="tiny", language="ru"), lambda f, m: steps.append(f))
    assert t.language == "ru" and t.model == "tiny" and t.duration_us == 4 * S
    assert [s.text for s in t.segments] == ["hello there", "second half"]
    assert t.segments[0].has_word_timing and not t.segments[1].has_word_timing
    assert steps[-1] == 1.0 and max(steps[:-1]) < 1.0
    engine.transcribe(audio, TranscribeOptions(model="tiny"))
    assert len(fake_whisper.calls) == 1  # model loaded once


def test_engine_falls_back_to_cpu(fake_whisper, tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "cuda_available", lambda: True)
    monkeypatch.setattr(engine_mod, "missing_cuda_libraries", lambda: [])
    fake_whisper.fail_on = {"cuda"}
    engine = WhisperEngine(tmp_path)
    t = engine.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), TranscribeOptions())
    assert engine.device_used == "cpu" and t.segments

    # CUDA loads but fails while running (missing cuBLAS): retried on CPU.
    fake_whisper.fail_on = {"runtime"}
    fake_whisper.calls = []
    engine = WhisperEngine(tmp_path)
    original = FakeModel.transcribe

    def flaky(self, audio, language=None, **kw):
        if self.device == "cuda":
            return original(self, audio, language, **kw)
        FakeModel.fail_on = set()
        return original(self, audio, language, **kw)

    monkeypatch.setattr(FakeModel, "transcribe", flaky)
    t = engine.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), TranscribeOptions())
    assert engine.device_used == "cpu" and t.segments
    assert [c[1] for c in fake_whisper.calls] == ["cuda", "cpu"]
    assert "GPU failed" in engine.notice and "cublas64_12.dll" in engine.notice

    # Same when the user explicitly picked the GPU.
    fake_whisper.fail_on = {"runtime"}
    fake_whisper.calls = []
    engine = WhisperEngine(tmp_path)
    t = engine.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), TranscribeOptions(device="cuda"))
    assert engine.device_used == "cpu" and t.segments

    fake_whisper.fail_on = {"cuda", "cpu"}
    with pytest.raises(TranscriptionError):
        WhisperEngine(tmp_path).transcribe(np.zeros(10, dtype=np.float32), TranscribeOptions())


def test_engine_cancel(fake_whisper, tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(TranscriptionCancelled):
        WhisperEngine(tmp_path).transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), TranscribeOptions(device="cpu"),
                                           cancel=cancel)


def test_decode_audio_and_transcribe_file(sample_clips, ffmpeg_paths, fake_whisper, tmp_path):
    audio = decode_audio(sample_clips["mp4"], ffmpeg_paths.ffmpeg)
    assert audio.dtype == np.float32
    assert len(audio) == pytest.approx(2 * SAMPLE_RATE, rel=0.05)
    assert 0.05 < np.abs(audio).max() <= 1.0
    with pytest.raises(TranscriptionError):
        decode_audio(sample_clips["mkv_noaudio"], ffmpeg_paths.ffmpeg)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(TranscriptionCancelled):
        decode_audio(sample_clips["mp4"], ffmpeg_paths.ffmpeg, cancel=cancel)
    t = transcribe_file(WhisperEngine(tmp_path), sample_clips["mp4"], ffmpeg_paths.ffmpeg,
                        TranscribeOptions(device="cpu"))
    assert t.duration_us == pytest.approx(2 * S, rel=0.05)


def test_downloaded_models(tmp_path):
    assert downloaded_models(tmp_path) == []
    snap = tmp_path / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    assert downloaded_models(tmp_path) == ["small"]


@pytest.mark.skipif(
    not (Path.home() / ".cache" / "tss-test-models").exists(),
    reason="real Whisper model not available (set up ~/.cache/tss-test-models with the tiny model)",
)
def test_real_model_on_speech(tmp_path, ffmpeg_paths):  # pragma: no cover - needs a downloaded model
    import shutil
    import subprocess

    if not shutil.which("espeak-ng"):
        pytest.skip("espeak-ng not installed")
    wav = tmp_path / "en.wav"
    subprocess.run(["espeak-ng", "-v", "en", "-w", str(wav), "Hello everyone, this is a test."], check=True)
    engine = WhisperEngine(Path.home() / ".cache" / "tss-test-models")
    t = transcribe_file(engine, wav, ffmpeg_paths.ffmpeg, TranscribeOptions(model="tiny", device="cpu"))
    assert t.language == "en"
    assert "test" in t.text.lower()
    assert t.exact_word_ratio > 0.5


# -------------------------------------------------------------------- UI

def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_panel_lists_seeks_and_edits(qapp):
    c = TranscriptController()
    panel = TranscriptPanel(c)
    panel.set_has_audio(True)
    assert panel.transcribe_button.isEnabled()
    assert "No transcript" in panel.info_label.text()

    c.set_transcript(sample_transcript())
    assert panel.tree.topLevelItemCount() == 3
    assert "Russian (97%)" in panel.info_label.text()
    approx = panel.tree.topLevelItem(1)
    assert approx.text(0).endswith("≈")

    seeks, words = [], []
    panel.seek_requested.connect(seeks.append)
    panel.word_selected.connect(lambda a, b: words.append((a, b)))
    panel._on_item_clicked(panel.tree.topLevelItem(2), 1)
    assert seeks == [7_230_000]
    word_item = panel.tree.topLevelItem(2).child(1)
    assert word_item.data(0, ROLE_KIND) == "word"
    panel._on_item_clicked(word_item, 1)
    assert words == [(7_500_000, 7_800_000)]
    panel._on_item_clicked(panel.tree.topLevelItem(1).child(0), 1)  # approximate word: no selection
    assert len(words) == 1

    # Editing in the tree updates the model.
    panel.tree.topLevelItem(0).setText(1, "Привет, народ!")
    assert c.transcript.segment(1).text == "Привет, народ!"
    assert c.transcript.segment(1).has_word_timing
    item = panel.tree.topLevelItem(2).child(2)
    item.setText(1, "unexpected?")
    assert c.transcript.segment(3).text == "That was unexpected?"
    # An invalid word edit is reverted.
    panel.tree.topLevelItem(2).child(0).setText(1, "")
    assert panel.tree.topLevelItem(2).child(0).text(1) == "That"

    panel.search.setText("unexpected")
    assert [panel.tree.topLevelItem(i).isHidden() for i in range(3)] == [True, True, False]
    panel.follow_playhead(5 * S)
    assert panel.tree.topLevelItem(1).font(0).bold()

    panel.set_busy(True, "Working")
    assert not panel.transcribe_button.isEnabled() and panel.cancel_button.isVisibleTo(panel)
    panel.set_busy(False)
    panel.set_downloaded_models(["small"])
    assert panel.model_combo.currentText() == "small  ✓"
    assert panel.options().model == "small"


@pytest.fixture
def window(qapp, data_dir, ffmpeg_paths, sample_clips, fake_whisper, monkeypatch):
    from ui.main_window.main_window import MainWindow

    monkeypatch.setattr(engine_mod, "cuda_available", lambda: False)
    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    w.resize(1400, 900)
    w.show()
    w.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: w.timeline.timeline is not None)
    yield w
    w._dirty = False
    w.close()


def test_window_transcribes_in_background_with_undo(window, tmp_path, data_dir, ffmpeg_paths):
    from ui.main_window.main_window import MainWindow

    window.transcript_panel.transcribe_button.click()
    assert _wait(lambda: window.transcript.transcript is not None)
    t = window.transcript.transcript
    assert [s.text for s in t.segments] == ["hello there", "second half"]
    assert "Done on CPU" in window.transcript_panel.status_label.text()
    assert window.timeline_panel.view._transcript is t
    assert window.project.transcript_for(str(window.preview.media.path)) == t

    # Clicking a word on the speech track selects it on the timeline.
    view = window.timeline_panel.view
    view.set_zoom(400)
    view.set_scroll_us(0)
    from ui.timeline.timeline_view import SPEECH_Y

    x = int(view.us_to_x(250_000))
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x, SPEECH_Y + 10))
    assert window.timeline.range == (100_000, 400_000)
    assert window.act_mute.isEnabled()

    window.transcript.edit_segment_text(1, "hello world")
    window.act_undo.trigger()
    assert window.transcript.transcript == t
    window.act_undo.trigger()
    assert window.transcript.transcript is None
    window.act_redo.trigger()
    assert window.transcript.transcript == t

    window.project_path = tmp_path / "t.tssproj"
    assert window.save()
    other = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    other.open_project(tmp_path / "t.tssproj")
    assert _wait(lambda: other.transcript.transcript is not None)
    assert other.transcript.transcript == t
    assert other.transcript_panel.tree.topLevelItemCount() == 2
    other._dirty = False
    other.close()


def test_window_reports_failure_and_cancel(window, fake_whisper, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    fake_whisper.fail_on = {"cpu"}
    window.transcript_panel.transcribe_button.click()
    assert _wait(lambda: bool(shown))
    assert "Could not load the speech model" in shown[0]
    assert window.transcript.transcript is None
    assert window.transcript_panel.transcribe_button.isEnabled()

    # Cancel: block the fake model until cancel is pressed.
    fake_whisper.fail_on = set()
    gate = threading.Event()
    original = FakeModel.transcribe

    def slow(self, audio, language=None, **kw):
        gate.wait(5)
        return original(self, audio, language, **kw)

    monkeypatch.setattr(FakeModel, "transcribe", slow)
    window.transcript_panel.transcribe_button.click()
    assert window.transcript_panel.cancel_button.isVisibleTo(window.transcript_panel)
    window.transcript_panel.cancel_button.click()
    gate.set()
    assert _wait(lambda: window._transcribe_task is None)
    assert window.transcript.transcript is None
    assert "Cancelled" in window.transcript_panel.status_label.text()

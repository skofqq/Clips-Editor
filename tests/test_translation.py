"""Offline subtitle translation (0.9.27): models, word timing, masking, project, panel, window."""

from __future__ import annotations

import sys
import types

import pytest

from core.projects.project import Project
from core.subtitles.model import SubtitleSettings, SubtitleStyle, build_captions
from core.timeline.model import MuteRegion, Timeline
from core.transcription.model import Segment, Transcript, Word
from core.translation.models import FILES, MODELS, direction_for, is_downloaded, model_dir
from core.translation.transcript import (
    is_translation,
    spread_words,
    translate_transcript,
    with_phrase,
)

S = 1_000_000


def _transcript() -> Transcript:
    a = (Word(0, 400_000, "привет", speaker=0), Word(500_000, 1_000_000, "ребята", speaker=0))
    b = (Word(2 * S, int(2.4 * S), "это"), Word(int(2.5 * S), 3 * S, "пиздец"))
    return Transcript("ru", 0.9, "small", (Segment(0, 0, S, "привет ребята", a),
                                            Segment(1, 2 * S, 3 * S, "это пиздец", b)), 4 * S)


FAKE = {"привет ребята": "hi guys, what's up", "это пиздец": "this is fucked"}


def fake_translate(lines, progress=None, cancel=None):
    if progress:
        progress(1.0, "")
    return [FAKE.get(line, line.upper()) for line in lines]


def test_models_and_directions(tmp_path):
    assert set(MODELS) == {"ru-en", "en-ru"} and MODELS["ru-en"].label == "Russian → English"
    assert direction_for("ru") == "ru-en" and direction_for("en") == "en-ru" and direction_for("") == "ru-en"
    folder = model_dir(tmp_path, "ru-en")
    assert not is_downloaded(tmp_path, "ru-en")
    folder.mkdir(parents=True)
    for name in FILES:
        (folder / name).write_bytes(b"x")
    assert not is_downloaded(tmp_path, "ru-en")  # no .complete mark yet
    (folder / ".complete").write_text("ok")
    assert is_downloaded(tmp_path, "ru-en")


def test_download_uses_hugging_face(tmp_path, monkeypatch):
    from core.translation import models

    calls = []

    def snapshot_download(repo, local_dir, allow_patterns):
        calls.append((repo, allow_patterns))
        for name in allow_patterns:
            (tmp_path / "translation" / "en-ru" / name).write_bytes(b"x")
        return local_dir

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=snapshot_download))
    seen = []
    folder = models.download_model(tmp_path, "en-ru", lambda f, m: seen.append(m))
    assert calls == [("quickmt/quickmt-en-ru", list(FILES))] and is_downloaded(tmp_path, "en-ru")
    assert folder == model_dir(tmp_path, "en-ru") and "400 MB" in seen[0]
    models.download_model(tmp_path, "en-ru")
    assert len(calls) == 1  # already there


def test_spread_words_keeps_the_phrase_time_and_speaker():
    segment = _transcript().segments[0]
    words = spread_words(segment, "hi guys, what's up")
    assert [w.text for w in words] == ["hi", "guys,", "what's", "up"]
    assert words[0].start_us == 0 and words[-1].end_us == 1_000_000
    assert all(a.end_us == b.start_us for a, b in zip(words, words[1:]))
    assert words[2].end_us - words[2].start_us > words[0].end_us - words[0].start_us  # longer word, more time
    assert {w.speaker for w in words} == {0}
    assert spread_words(segment, "   ") == ()


def test_translate_transcript_and_edit():
    original = _transcript()
    translated = translate_transcript(original, fake_translate, "ru-en")
    assert is_translation(translated) and not is_translation(original) and not is_translation(None)
    assert translated.language == "en" and translated.model == "translate:ru-en"
    assert [s.text for s in translated.segments] == ["hi guys, what's up", "this is fucked"]
    assert [s.id for s in translated.segments] == [0, 1]
    fixed = with_phrase(translated, 1, "this is  insane")
    assert fixed.segments[1].text == "this is insane" and fixed.segments[1].edited
    assert [w.text for w in fixed.segments[1].words] == ["this", "is", "insane"]
    assert Transcript.from_dict(fixed.to_dict()) == fixed


def test_translated_swears_are_masked_where_the_phrase_was_muted():
    translated = translate_transcript(_transcript(), fake_translate, "ru-en")
    timeline = Timeline.for_source(4 * S)
    style = SubtitleStyle(enabled=True, uppercase=False)
    plain = build_captions(translated, timeline, style)
    assert "fucked" in " ".join(c.text for c in plain)
    muted = timeline.add_mute(int(2.5 * S), 3 * S) if hasattr(timeline, "add_mute") else None
    if muted is None:
        from dataclasses import replace

        muted = replace(timeline, mutes=(MuteRegion(1, int(2.5 * S), 3 * S),))
    text = " ".join(c.text for c in build_captions(translated, muted, style))
    assert "f*****" in text and "this is" in text and "fucked" not in text


def test_project_keeps_translations(tmp_path):
    project = Project()
    translated = translate_transcript(_transcript(), fake_translate, "ru-en")
    project.set_translation("C:/clips/a.mp4", translated)
    project.subtitles = SubtitleSettings(SubtitleStyle(enabled=True), translated=True)
    loaded = Project.from_dict(project.to_dict())
    assert loaded.translation_for("c:/clips/A.mp4") == translated and loaded.subtitles.translated
    assert SubtitleSettings.from_dict({}).translated is False
    loaded.remove_media("C:/clips/a.mp4")
    assert loaded.translation_for("C:/clips/a.mp4") is None
    broken = project.to_dict()
    broken["translations"] = [{"media": "x", "segments": [{"id": "a"}]}]
    assert Project.from_dict(broken).translations == {}  # a bad translation never blocks the project


def test_translator_engine_with_fake_libraries(tmp_path, monkeypatch):
    class Processor:
        def __init__(self, model_file):
            self.file = model_file

        def encode(self, text, out_type=str):
            return ["▁" + w for w in text.split()]

        def decode(self, tokens):
            return " ".join(t.strip("▁") for t in tokens)

    batches = []

    class FakeTranslator:
        def __init__(self, path, device, compute_type):
            if device == "cuda":
                raise RuntimeError("no cuBLAS")
            self.device, self.compute_type = device, compute_type

        def translate_batch(self, tokens, beam_size, max_decoding_length):
            batches.append(len(tokens))
            return [types.SimpleNamespace(hypotheses=[[t.upper() for t in toks]]) for toks in tokens]

    monkeypatch.setitem(sys.modules, "sentencepiece", types.SimpleNamespace(SentencePieceProcessor=Processor))
    monkeypatch.setitem(sys.modules, "ctranslate2", types.SimpleNamespace(
        Translator=FakeTranslator, get_cuda_device_count=lambda: 1))
    from core.translation.engine import BATCH, Translator

    translator = Translator(tmp_path)
    assert translator.device == "cpu" and translator.model.compute_type == "int8"  # GPU failed: CPU
    lines = ["привет мир", "", "  "] + [f"фраза {i}" for i in range(BATCH + 2)]
    out = translator.translate(lines)
    assert out[0] == "▁ПРИВЕТ ▁МИР".replace("▁", "") or out[0] == "ПРИВЕТ МИР"
    assert out[1] == "" and out[2] == "" and len(out) == len(lines)
    assert batches == [BATCH, 3]
    import threading

    stop = threading.Event()
    stop.set()
    from core.translation.models import TranslationCancelled

    with pytest.raises(TranslationCancelled):
        translator.translate(["a"], cancel=stop)


def test_panel_translation_controls(qapp):
    from ui.subtitles.subtitles_panel import SubtitlesPanel

    panel = SubtitlesPanel()
    asked, seen = [], []
    panel.translate_requested.connect(asked.append)
    panel.settings_changed.connect(seen.append)
    panel.set_translation(None, "en")
    assert panel.direction_combo.currentData() == "en-ru" and not panel.use_translation_check.isEnabled()
    panel.translate_button.click()
    assert asked == ["en-ru"]
    panel.set_translation_busy(True, "Downloading…")
    assert panel.translate_button.isHidden() and not panel.translate_cancel.isHidden()
    panel.set_translation_progress(0.5, "Half")
    assert panel.translate_progress.value() == 500 and panel.translation_label.text() == "Half"
    panel.set_translation_busy(False)
    translated = translate_transcript(_transcript(), fake_translate, "ru-en")
    panel.set_translation(translated, "ru")
    assert panel.use_translation_check.isEnabled() and "Russian → English" in panel.translation_label.text()
    panel.use_translation_check.setChecked(True)
    assert seen[-1].translated
    panel.set_settings(SubtitleSettings(translated=False))
    assert not panel.use_translation_check.isChecked()
    panel.deleteLater()


def test_translation_dialog(qapp):
    from ui.subtitles.translation_dialog import TranslationDialog

    original = _transcript()
    translated = translate_transcript(original, fake_translate, "ru-en")
    dialog = TranslationDialog(original, translated)
    assert dialog.table.rowCount() == 2 and dialog.table.item(1, 1).text() == "это пиздец"
    assert dialog.changes() == {}
    dialog.table.item(1, 2).setText("this is  wild")
    assert dialog.changes() == {1: "this is wild"}
    assert dialog.edited().segments[1].text == "this is wild"
    dialog.deleteLater()


def test_window_translation_flow(qapp, data_dir, ffmpeg_paths, sample_clips, monkeypatch):
    from app.config import AppConfig
    from core.translation import transcript as module
    from tests.test_subtitles import _clip_transcript, _wait
    from ui.main_window.main_window import MainWindow

    def fake_file(models_dir, transcript, direction, progress=None, cancel=None):
        progress(0.5, "Translating…")
        return translate_transcript(transcript, lambda lines, **_k: [f"en {line}" for line in lines], direction)

    monkeypatch.setattr(module, "translate_file", fake_file)
    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    try:
        w.import_files([str(sample_clips["mp4"])])
        assert _wait(lambda: w.timeline.timeline is not None)
        w.transcript.set_transcript(_clip_transcript())
        panel = w.subtitles_panel
        panel.enabled_check.setChecked(True)
        original = [c.text for c in w.captions]
        assert w._captions_language() == "ru" and not panel.edit_translation_button.isEnabled()
        assert w.translate_subtitles()
        assert _wait(lambda: w.current_translation() is not None and w._translate_task is None)
        assert panel.settings().translated and w.project.subtitles.translated
        assert w.captions and w.captions[0].words[0].text == "EN" and [c.text for c in w.captions] != original
        assert w._captions_language() == "en" and panel.edit_translation_button.isEnabled()
        panel.use_translation_check.setChecked(False)
        assert [c.text for c in w.captions] == original and w._captions_language() == "ru"
    finally:
        w._dirty = False
        w.close()

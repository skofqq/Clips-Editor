"""GigaAM / Parakeet through onnx-asr, and the swear-word double check (no real models needed)."""

from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest

from core.censor.dictionary import Dictionary
from core.censor.review import CHECK, CHECK_SEGMENT, ACCEPTED, Suggestion, detect
from core.transcription import engine as eng
from core.transcription import onnx_engine
from core.transcription.model import Segment, Transcript, Word

RATE = onnx_engine.SAMPLE_RATE


def test_tokens_are_grouped_into_timed_words():
    tokens = [" при", "вет", ",", " бля", "ть", " "]
    stamps = [0.0, 0.2, 0.3, 0.8, 1.0, 1.1]
    words = onnx_engine.tokens_to_words(tokens, stamps, [-0.1] * 6, offset_s=10.0, chunk_end_s=12.0, frame_s=0.04)
    assert [w.text for w in words] == ["привет,", "блять"]
    first, second = words
    assert first.start_us == 10_000_000 and second.start_us == 10_800_000
    assert first.end_us <= second.start_us  # never runs into the next word
    assert second.end_us <= 12_000_000 and second.end_us > 11_000_000
    assert 0.8 < first.probability <= 1.0


def test_late_punctuation_does_not_stretch_a_word():
    tokens = [" так", ",", " вод", "ичка", "."]
    stamps = [1.7, 1.9, 2.1, 2.4, 9.8]  # the full stop comes at the end of a long pause
    words = onnx_engine.tokens_to_words(tokens, stamps, None, 0.0, 9.84, 0.04)
    assert [w.text for w in words] == ["так,", "водичка."]
    assert words[1].end_us < 3_000_000


def test_tokens_without_timestamps_give_no_words():
    assert onnx_engine.tokens_to_words(["a"], None, None, 0, 1, 0.04) == []


def test_speech_chunks_respect_the_model_limit():
    audio = np.zeros(RATE * 47, dtype=np.float32)
    chunks = onnx_engine.speech_chunks(audio, use_vad=False)
    assert chunks[0] == (0, 20 * RATE) and chunks[-1][1] == len(audio)
    assert all(end - start <= 20 * RATE for start, end in chunks)
    assert onnx_engine.speech_chunks(np.zeros(0, dtype=np.float32), use_vad=False) == []


@dataclass
class FakeResult:
    tokens: list
    timestamps: list
    logprobs: list


class FakeAsr:
    def __init__(self):
        self.calls = 0

    def recognize(self, chunk, sample_rate):
        assert sample_rate == RATE
        self.calls += 1
        return FakeResult([" сука", " да"], [0.1, 0.6], [0.0, 0.0])


def test_onnx_engine_builds_a_transcript(tmp_path, monkeypatch):
    fake = FakeAsr()
    monkeypatch.setattr(onnx_engine, "load_model", lambda *a, **k: fake)
    audio = np.zeros(RATE * 25, dtype=np.float32)
    t = onnx_engine.OnnxEngine(tmp_path).transcribe(audio, "gigaam-v3", use_vad=False)
    assert fake.calls == 2 and t.language == "ru" and t.model == "gigaam-v3"
    assert [s.text for s in t.segments] == ["сука да", "сука да"]
    assert t.segments[1].words[0].start_us == 20_100_000


def test_parakeet_guesses_the_language_from_letters(tmp_path, monkeypatch):
    class English(FakeAsr):
        def recognize(self, chunk, sample_rate):
            return FakeResult([" what", " the", " fuck"], [0.0, 0.3, 0.5], [0.0] * 3)

    monkeypatch.setattr(onnx_engine, "load_model", lambda *a, **k: English())
    t = onnx_engine.OnnxEngine(tmp_path).transcribe(np.zeros(RATE * 3, dtype=np.float32), "parakeet-v3",
                                                    use_vad=False)
    assert t.language == "en" and t.text == "what the fuck"


def test_whisper_engine_sends_onnx_models_to_onnx(tmp_path, monkeypatch):
    fake = FakeAsr()
    monkeypatch.setattr(onnx_engine, "load_model", lambda *a, **k: fake)
    engine = eng.WhisperEngine(tmp_path)
    t = engine.transcribe(np.zeros(RATE * 2, dtype=np.float32),
                          eng.TranscribeOptions(model="gigaam-v3", vad=False))
    assert t.model == "gigaam-v3" and engine.device_used == "cpu"


def test_downloaded_models_include_onnx_and_distil(tmp_path):
    folder = onnx_engine.model_dir(tmp_path, "gigaam-v3")
    folder.mkdir(parents=True)
    assert "gigaam-v3" not in eng.downloaded_models(tmp_path)  # unfinished download
    (folder / onnx_engine.COMPLETE_MARK).write_text("int8")
    snap = tmp_path / "models--Systran--faster-distil-whisper-large-v3" / "snapshots" / "r1"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    have = eng.downloaded_models(tmp_path)
    assert "gigaam-v3" in have and "distil-large-v3" in have and "parakeet-v3" not in have
    for name in ("gigaam-v3", "parakeet-v3", "distil-large-v3"):
        assert name in eng.MODEL_SIZES and not eng.is_custom_model(name)


def test_check_model_is_of_the_other_kind(tmp_path):
    assert eng.check_model_for("large-v3-russian-int8", "ru", tmp_path) == "gigaam-v3"
    assert eng.check_model_for("small", "en", tmp_path) == "parakeet-v3"
    assert eng.check_model_for("gigaam-v3", "ru", tmp_path) == "small"  # nothing better downloaded
    snap = tmp_path / "models--bzikst--faster-whisper-large-v3-russian-int8" / "snapshots" / "r"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    assert eng.check_model_for("gigaam-v3", "ru", tmp_path) == "large-v3-russian-int8"


def _transcript(words, check=()):
    seg = Segment(1, words[0].start_us, words[-1].end_us, " ".join(w.text for w in words), tuple(words))
    return Transcript("ru", 1.0, "small", (seg,), 10_000_000, "gigaam-v3" if check else "", tuple(check))


def test_add_cross_check_keeps_the_second_models_words(tmp_path, monkeypatch):
    main = _transcript([Word(1_000_000, 1_400_000, "сухо")])

    class Engine:
        models_dir = tmp_path
        device_used, notice = "cuda", ""

        def transcribe(self, audio, options, progress, cancel):
            assert options.model == "gigaam-v3" and not options.cross_check
            return _transcript([Word(1_000_000, 1_400_000, "сука")])

    audio = np.zeros(RATE * 2, dtype=np.float32)
    out = eng.add_cross_check(Engine(), main, audio, eng.TranscribeOptions(model="small", refine_timing=False))
    assert out.check_model == "gigaam-v3" and [w.text for w in out.check_words] == ["сука"]
    assert out.segments == main.segments
    assert Transcript.from_dict(out.to_dict()) == out


def test_censor_adds_words_only_the_second_model_heard():
    words = [Word(1_000_000, 1_400_000, "сухо"), Word(3_000_000, 3_300_000, "блять")]
    check = [Word(1_010_000, 1_390_000, "сука"), Word(3_020_000, 3_310_000, "блять"),
             Word(6_000_000, 6_300_000, "привет")]
    state = detect(_transcript(words, check), Dictionary())
    texts = {(s.text, s.source) for s in state.suggestions}
    assert texts == {("блять", ""), ("сука", CHECK)}  # the shared word is not listed twice
    extra = next(s for s in state.suggestions if s.source == CHECK)
    assert extra.segment_id == CHECK_SEGMENT
    # decisions survive a re-detection and a save
    state = state.with_status([extra.id], ACCEPTED)
    again = detect(_transcript(words, check), Dictionary(), previous=state)
    assert next(s for s in again.suggestions if s.source == CHECK).status == ACCEPTED
    assert Suggestion.from_dict(extra.to_dict()) == extra


def test_onnx_download_marks_the_model_complete(tmp_path, monkeypatch):
    tried = []

    def fake_load(model, folder, quantization):
        tried.append(quantization)
        folder.mkdir(parents=True, exist_ok=True)
        if quantization == "int8":
            raise FileNotFoundError("no int8 files")
        return SimpleNamespace(with_timestamps=lambda: "asr")

    pytest.importorskip("onnx_asr")
    monkeypatch.setattr(onnx_engine, "_load", fake_load)
    assert onnx_engine.load_model(tmp_path, "parakeet-v3") == "asr"
    assert tried == ["int8", None]
    assert onnx_engine.is_downloaded(tmp_path, "parakeet-v3")
    assert onnx_engine.load_model(tmp_path, "parakeet-v3") == "asr"  # loads from disk now
    assert tried == ["int8", None, None]

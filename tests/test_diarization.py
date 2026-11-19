"""Speakers: features, clustering, word labels, storage, subtitles, UI."""

from __future__ import annotations

import time

import numpy as np
import pytest

from core.subtitles.model import SubtitleStyle, build_captions, speaker_text_color
from core.timeline.model import Timeline
from core.transcription import diarization
from core.transcription.diarization import cluster, fbank, find_speakers
from core.transcription.model import Segment, Transcript, Word

S = 1_000_000
RATE = 16000


class ToneEmbedder:
    """Fake speaker model: the 'voice' is the dominant pitch of the window."""

    def embed(self, samples):
        spectrum = np.abs(np.fft.rfft(samples))
        pitch = np.argmax(spectrum) * RATE / samples.size
        vec = np.array([np.cos(pitch / 100), np.sin(pitch / 100), 0.1])
        return vec / np.linalg.norm(vec)


def _conversation():
    """A (200 Hz) speaks 0–3 s, B (450 Hz) 3–6 s, A again 6–9 s; one word per 0.5 s."""
    t = np.arange(9 * RATE) / RATE
    pitch = np.where((t >= 3) & (t < 6), 450.0, 200.0)
    audio = (0.3 * np.sin(2 * np.pi * pitch * t)).astype(np.float32)
    words = tuple(Word(int(k * 0.5 * S), int((k * 0.5 + 0.4) * S), f"w{k}") for k in range(18))
    segments = (Segment(1, 0, 3 * S, "a", words[:6]), Segment(2, 3 * S, 6 * S, "b", words[6:12]),
                Segment(3, 6 * S, 9 * S, "c", words[12:]))
    return Transcript("ru", 1.0, "small", segments, 9 * S), audio


def test_fbank_shape_and_normalisation():
    feats = fbank(np.random.default_rng(0).standard_normal(RATE).astype(np.float32) * 0.1)
    assert feats.shape == (98, 80)
    assert np.abs(feats.mean(axis=0)).max() < 1e-3


def test_cluster_auto_and_fixed():
    a, b = np.array([1.0, 0.0]), np.array([0.0, 1.0])
    emb = np.stack([a, a * 0.99 + b * 0.05, b, b, a])
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    assert cluster(emb) == [0, 0, 1, 1, 0]
    assert cluster(emb, 1) == [0] * 5
    assert len(set(cluster(emb, 3))) == 3
    assert cluster(np.zeros((0, 2))) == []


def test_words_get_the_right_speaker():
    transcript, audio = _conversation()
    labels = find_speakers(transcript, audio, ToneEmbedder())
    assert labels[:5] == [0] * 5 and labels[7:11] == [1] * 4 and labels[13:] == [0] * 5
    assert find_speakers(transcript, audio, ToneEmbedder(), speakers=2) == labels
    empty = Transcript("ru", 1.0, "small", ())
    assert find_speakers(empty, audio, ToneEmbedder()) == []


def test_transcript_stores_speakers():
    transcript, _ = _conversation()
    labelled = transcript.with_speakers([0] * 6 + [1] * 6 + [0] * 6)
    assert labelled.speaker_count == 2 and transcript.speaker_count == 0
    again = Transcript.from_dict(labelled.to_dict())
    assert again == labelled
    assert len(transcript.to_dict()["segments"][0]["words"][0]) == 5  # old format when unknown
    rewritten = labelled.with_segment_text(2, "совсем другой текст")
    assert {w.speaker for w in rewritten.segment(2).words} == {1}


def test_captions_split_and_colour_by_speaker():
    transcript, _ = _conversation()
    labelled = transcript.with_speakers([0] * 6 + [1] * 6 + [0] * 6)
    style = SubtitleStyle(enabled=True, max_words=12, max_chars=80, gap_split_ms=5000)
    caps = build_captions(labelled, Timeline.for_source(9 * S), style)
    assert [len(c.words) for c in caps[:3]] == [6, 6, 6]
    assert {w.speaker for w in caps[1].words} == {1}
    assert speaker_text_color(style, 0) == style.color and speaker_text_color(style, 1) != style.color
    plain = SubtitleStyle(enabled=True, speaker_colors=False)
    assert speaker_text_color(plain, 1) == plain.color
    assert SubtitleStyle.from_dict(plain.to_dict()) == plain


def test_window_find_speakers(qapp, data_dir, ffmpeg_paths, sample_clips, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow

    monkeypatch.setattr(diarization, "find_speakers_in_file",
                        lambda models, path, ffmpeg, t, count, progress=None, cancel=None:
                        [k % 2 for k in range(len(t.words()))])
    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    w.show()
    w.import_files([str(sample_clips["mp4"])])

    def wait(pred, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            QApplication.processEvents()
            if pred():
                return True
            time.sleep(0.02)
        return False

    assert wait(lambda: w.timeline.timeline is not None)
    panel = w.transcript_panel
    assert not panel.speakers_button.isEnabled()
    transcript, _ = _conversation()
    w.transcript.set_transcript(transcript)
    assert panel.speakers_button.isEnabled()
    panel.speakers_combo.setCurrentIndex(panel.speakers_combo.findData(2))
    panel.speakers_button.click()
    assert wait(lambda: w._transcribe_task is None)
    assert w.transcript.transcript.speaker_count == 2
    assert "Found 2 speakers" in panel.status_label.text()
    assert panel.tree.topLevelItem(0).text(0).startswith("S1")
    w.act_undo.trigger()
    assert w.transcript.transcript.speaker_count == 0
    w._dirty = False
    w.close()


def test_errors_are_transcription_errors(tmp_path, monkeypatch):
    from core.audio import download
    from core.transcription.engine import TranscriptionError

    def offline(*a, **k):
        raise download.DownloadError("offline")

    monkeypatch.setattr(download, "fetch", offline)
    with pytest.raises(TranscriptionError, match="offline"):
        diarization.download_model(tmp_path)

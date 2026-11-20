"""Highlight finder: loud moments, laughter/shouting in words, grouping, UI."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from core.audio.waveform import Waveform
from core.highlights.finder import (
    HYPE,
    LAUGH,
    LOUD,
    SHOUT,
    Event,
    Highlight,
    find_highlights,
    group,
    loud_events,
    loudness_curve,
    word_events,
    word_reasons,
)
from core.projects.project import Project
from core.transcription.model import WORD_TIMING, Segment, Transcript, Word

S = 1_000_000
PPS = 100


def _peaks(seconds: float, level: float, bursts: list[tuple[float, float, float]]) -> np.ndarray:
    """Steady talking at ``level`` (0..1) with louder bursts (start s, end s, level)."""
    rng = np.random.default_rng(1)
    x = np.full(int(seconds * PPS), level) * rng.uniform(0.6, 1.0, int(seconds * PPS))
    for start, end, peak in bursts:
        x[int(start * PPS):int(end * PPS)] = peak
    return np.clip(x * 255, 0, 255).astype(np.uint8)


def _transcript(words: list[tuple[float, str]]) -> Transcript:
    segs = []
    for i, (t, text) in enumerate(words):
        start = int(t * S)
        segs.append(Segment(i + 1, start, start + 400_000, text,
                            (Word(start, start + 400_000, text, 0.9, WORD_TIMING),)))
    return Transcript(language="ru", language_probability=1.0, model="test", segments=tuple(segs),
                      duration_us=60 * S)


def test_loudness_curve_levels():
    times, db = loudness_curve(np.full(300, 255, np.uint8), PPS)
    assert times[0] == pytest.approx(0.15) and db.max() == pytest.approx(0.0, abs=0.1)
    _, quiet = loudness_curve(np.full(300, 26, np.uint8), PPS)
    assert quiet.mean() == pytest.approx(-19.8, abs=0.3)


def test_loud_events_find_shouts_not_talking():
    peaks = _peaks(60, 0.08, [(20.0, 21.5, 0.9), (45.0, 45.8, 0.7)])
    events = loud_events(peaks, PPS)
    assert [round(e.t_us / S) for e in events] == [20, 45]
    assert all(e.reason == LOUD for e in events) and all(0 < e.score <= 1 for e in events)
    assert loud_events(_peaks(60, 0.08, []), PPS) == []
    # Fewer moments at low sensitivity, more at high.
    small = _peaks(60, 0.08, [(30.0, 31.0, 0.25)])
    assert loud_events(small, PPS, "low") == [] and loud_events(small, PPS, "high")


@pytest.mark.parametrize("text, reason", [
    ("ахахаха", LAUGH), ("АХАХАХ", LAUGH), ("хахаха", LAUGH), ("lol", LAUGH), ("LMAO", LAUGH),
    ("[смех]", LAUGH), ("(laughs)", LAUGH), ("кек", LAUGH),
    ("дааааа", SHOUT), ("noooo", SHOUT), ("стой!", SHOUT), ("ЧТО", SHOUT),
    ("жесть", HYPE), ("клатч", HYPE), ("вау", HYPE), ("omg", HYPE),
])
def test_word_cues(text, reason):
    assert reason in [r for r, _ in word_reasons(text)]


@pytest.mark.parametrize("text", ["привет", "сегодня", "hello", "игра", "добро", "кстати", "Ха"])
def test_ordinary_words_are_not_cues(text):
    assert word_reasons(text) == []


def test_group_ranges_and_scores():
    events = [Event(10 * S, 0.7, LAUGH), Event(12 * S, 0.6, LOUD), Event(40 * S, 0.4, HYPE),
              Event(50 * S, 0.55, SHOUT)]
    found = group(events, 60 * S)
    assert [h.start_us for h in found] == [5 * S, 45 * S]  # the lone hype word is dropped
    first = found[0]
    assert first.end_us == 15 * S and first.peak_us == 10 * S
    assert first.score == pytest.approx(1 - 0.3 * 0.4) and first.reasons == (LAUGH, LOUD)
    assert first.label == "Laughter, Loud"
    # Overlapping ranges merge; ranges stay inside the clip.
    near = group([Event(1 * S, 0.9, LOUD), Event(6 * S, 0.9, LOUD)], 7 * S)
    assert len(near) == 1 and near[0].start_us == 0 and near[0].end_us == 7 * S


def test_find_highlights_combines_sound_and_words():
    waveform = Waveform(_peaks(60, 0.08, [(30.0, 31.0, 0.9)]), PPS)
    transcript = _transcript([(5.0, "привет"), (30.2, "ахахаха"), (31.0, "жесть")])
    found = find_highlights(waveform, transcript, 60 * S)
    assert len(found) == 1
    assert set(found[0].reasons) >= {LOUD, LAUGH} and found[0].score > 0.8
    assert found[0].start_us < 30 * S < found[0].end_us
    assert find_highlights(None, None, 60 * S) == []
    assert word_events(None) == []


def test_highlights_saved_in_project():
    h = Highlight(1 * S, 5 * S, 3 * S, 0.8, (LOUD, LAUGH))
    project = Project()
    project.set_highlights("C:/clip.mp4", [h])
    loaded = Project.from_dict(project.to_dict())
    assert loaded.highlights_for("c:/clip.mp4") == (h,)
    data = project.to_dict()
    data["highlights"][0]["items"].append({"start_us": 5, "end_us": 1})  # broken entry
    assert Project.from_dict(data).highlights == {}
    loaded.remove_media("C:/clip.mp4")
    assert loaded.highlights == {}


# ------------------------------------------------------------------ window

@pytest.fixture(scope="module")
def shout_clip(tmp_path_factory, ffmpeg_paths) -> Path:
    """12 s of quiet 'talk' with a loud burst at 6-7 s."""
    path = tmp_path_factory.mktemp("hl") / "shout.mp4"
    subprocess.run([
        str(ffmpeg_paths.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=12",
        "-filter_complex", "[1:a]volume='if(between(t,6,7),0.9,0.03)':eval=frame[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    ], check=True)
    return path


def test_window_finds_and_uses_highlights(qapp, data_dir, ffmpeg_paths, shout_clip):
    from app.config import AppConfig
    from tests.test_ui import _wait
    from ui.main_window.main_window import MainWindow

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    w.import_files([str(shout_clip)])
    assert _wait(lambda: w.timeline.timeline is not None)
    panel = w.highlights_panel
    assert panel.find_button.isEnabled() and "transcribe" in panel.status.text().lower()
    panel.find_button.click()
    assert _wait(lambda: panel.list.count() > 0, 20)
    h = w.highlights.highlights[0]
    assert h.start_us < 6_500_000 < h.end_us and LOUD in h.reasons
    assert w.timeline_panel.view._highlights == w.highlights.highlights
    assert w.project.highlights_for(str(shout_clip))

    panel.select_button.click()
    assert w.timeline.range == (h.start_us, h.end_us)
    # Clicking the orange marker on the ruler selects it too.
    view = w.timeline_panel.view
    w.timeline.set_range(None)
    hit = view.hit_test(__import__("PySide6.QtCore", fromlist=["QPointF"]).QPointF(view.us_to_x(h.peak_us), 19))
    assert hit == {"area": "highlight", "index": 0}
    view.highlight_clicked.emit(0)
    assert w.timeline.range == (h.start_us, h.end_us)

    panel.keep_button.click()
    t = w.timeline.timeline
    assert (t.start_us, t.end_us) == (h.start_us, h.end_us)
    w.act_undo.trigger()
    assert w.timeline.timeline.output_us == w.timeline.timeline.duration_us
    w._set_dirty(False)
    w.close()

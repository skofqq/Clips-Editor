from __future__ import annotations

from fractions import Fraction

import numpy as np
import pytest

from core.audio.waveform import Waveform, cache_file, load_waveform, peaks_from_pcm
from core.history import History
from core.projects.project import Project, ProjectFormatError, load_project, save_project
from core.timeline.model import BEEP, SILENCE, MuteRegion, Segment, Timeline
from core.timeline.timebase import (
    format_timecode,
    frame_to_us,
    sample_to_us,
    seconds_to_us,
    snap_to_frame,
    us_to_frame,
    us_to_sample,
)

S = 1_000_000


# ---------------------------------------------------------------- timebase

@pytest.mark.parametrize("fps", [Fraction(30), Fraction(60), Fraction(30000, 1001), Fraction(24000, 1001), Fraction(25)])
def test_frame_roundtrip_has_no_drift(fps):
    # Three hours of frames: converting there and back never slips a frame.
    for frame in list(range(0, 2000)) + [10**5, 3 * 3600 * 60, 648_000 - 1]:
        assert us_to_frame(frame_to_us(frame, fps), fps) == frame


def test_time_conversions():
    assert seconds_to_us(1.5) == 1_500_000
    assert frame_to_us(1, Fraction(30000, 1001)) == 33367
    assert snap_to_frame(40_000, Fraction(30)) == 33333
    assert us_to_sample(S, 48000) == 48000
    assert sample_to_us(us_to_sample(123_456_000, 48000), 48000) == pytest.approx(123_456_000, abs=21)
    assert format_timecode(3_725_004_000) == "1:02:05.004"
    assert format_timecode(61_500_000, show_ms=False) == "01:01"


# ------------------------------------------------------------------- model

def test_new_timeline_keeps_whole_clip():
    t = Timeline.for_source(10 * S)
    assert t.segments == (Segment(0, 10 * S),)
    assert t.output_us == 10 * S


def test_split_trim_delete():
    t = Timeline.for_source(10 * S).split_at(4 * S).split_at(7 * S)
    assert [(s.start_us, s.end_us) for s in t.segments] == [(0, 4 * S), (4 * S, 7 * S), (7 * S, 10 * S)]
    assert t.output_us == 10 * S  # splitting alone changes nothing

    t2 = t.delete_segment(1)
    assert t2.output_us == 7 * S
    assert t2.source_to_output(8 * S) == 5 * S
    assert t2.source_to_output(5 * S) == 4 * S  # inside the cut → the cut point
    assert t2.output_to_source(5 * S) == 8 * S
    assert t2.next_kept(5 * S) == 7 * S
    assert t2.next_kept(9 * S) == 9 * S
    assert t2.next_kept(10 * S) is None

    # Trimming cannot cross a neighbour or leave the source.
    t3 = t2.trim_segment(1, start_us=2 * S)
    assert t3.segments[1].start_us == 4 * S
    t3 = t2.trim_segment(0, start_us=-5, end_us=99 * S)
    assert t3.segments[0] == Segment(0, 7 * S)
    t3 = t2.trim_segment(0, end_us=1 * S).trim_segment(0, start_us=5 * S)
    assert t3.segments[0].length_us == 40_000  # minimum length kept

    # Splitting too close to an edge, in a gap, or deleting the last segment: no-ops.
    assert t2.split_at(5 * S) is t2
    assert t.split_at(4 * S + 10) is t
    only = Timeline.for_source(S)
    assert only.delete_segment(0) is only


def test_delete_and_keep_range():
    t = Timeline.for_source(10 * S).delete_range(3 * S, 5 * S)
    assert [(s.start_us, s.end_us) for s in t.segments] == [(0, 3 * S), (5 * S, 10 * S)]
    t = t.delete_range(4 * S, 6 * S)  # overlaps a gap and a segment
    assert t.segments[1] == Segment(6 * S, 10 * S)
    assert t.delete_range(0, 10 * S) is t  # refusing to delete everything
    kept = Timeline.for_source(10 * S).keep_range(2 * S, 6 * S)
    assert kept.segments == (Segment(2 * S, 6 * S),)
    assert kept.restore_all().output_us == 10 * S


def test_mute_regions():
    t = Timeline.for_source(10 * S)
    t = t.add_mute(2 * S, 3 * S).add_mute(6 * S, 5 * S, BEEP)  # reversed range is fine
    assert [(m.id, m.start_us, m.end_us, m.mode) for m in t.mutes] == [
        (1, 2 * S, 3 * S, SILENCE), (2, 5 * S, 6 * S, BEEP),
    ]
    assert t.mute_at(2_500_000).id == 1 and t.mute_at(3 * S) is None
    assert t.add_mute(S, S + 5) is t  # too short
    assert t.add_mute(-5 * S, 20 * S).mutes[0].length_us == 10 * S  # clamped

    t = t.update_mute(1, end_us=4 * S, mode=BEEP)
    assert t.mute(1) == MuteRegion(1, 2 * S, 4 * S, BEEP)
    t = t.update_mute(1, start_us=99 * S)  # cannot invert
    assert t.mute(1).start_us == 4 * S - 20_000
    t = t.move_mute(2, 9_800_000)
    assert t.mute(2).end_us == 10 * S and t.mute(2).length_us == S
    assert t.delete_mute(1).mute(1) is None
    assert t.delete_mute(42) is t


def test_merged_mutes_keeps_originals():
    t = Timeline.for_source(10 * S)
    t = t.add_mute(1 * S, 2 * S).add_mute(1_500_000, 3 * S).add_mute(3 * S, 4 * S)
    t = t.add_mute(3_500_000, 5 * S, BEEP).add_mute(8 * S, 9 * S)
    assert len(t.mutes) == 5
    assert t.merged_mutes() == [(1 * S, 4 * S, SILENCE), (3_500_000, 5 * S, BEEP), (8 * S, 9 * S, SILENCE)]
    merged = t.merge_mutes()
    assert len(merged.mutes) == 3
    assert len({m.id for m in merged.mutes}) == 3


def test_volume_gain_fades_and_mutes():
    t = Timeline.for_source(10 * S).with_gain_db(-6.0).with_fades(2 * S, 1 * S)
    assert t.gain_linear == pytest.approx(0.501, abs=0.001)
    assert t.volume_at(0) == 0
    assert t.volume_at(1 * S) == pytest.approx(t.gain_linear * 0.5)
    assert t.volume_at(5 * S) == pytest.approx(t.gain_linear)
    assert t.volume_at(9_500_000) == pytest.approx(t.gain_linear * 0.5)
    t = t.add_mute(4 * S, 6 * S)
    assert t.volume_at(5 * S) == 0
    # Fades follow the edited output, not the source.
    cut = Timeline.for_source(10 * S).with_fades(2 * S, 0).keep_range(4 * S, 10 * S)
    assert cut.volume_at(5 * S) == pytest.approx(0.5)
    assert cut.volume_at(2 * S) == 0  # cut away
    assert Timeline.for_source(S).with_gain_db(999).gain_db == 20.0
    assert Timeline.for_source(S).with_fades(5 * S, 5 * S).fade_in_us == S


def test_timeline_roundtrip_and_validation():
    t = Timeline.for_source(10 * S).delete_range(3 * S, 5 * S).add_mute(S, 2 * S, BEEP)
    t = t.with_gain_db(3.5).with_fades(500_000, 250_000)
    assert Timeline.from_dict(t.to_dict()) == t
    good = t.to_dict()
    for bad in (
        {**good, "segments": []},
        {**good, "segments": [[5, 4]]},
        {**good, "segments": [[0, 5 * S], [4 * S, 6 * S]]},
        {**good, "segments": [[0, 99 * S]]},
        {**good, "mutes": [{"id": 1, "start_us": 5, "end_us": 1}]},
        {**good, "mutes": [{"id": 1, "start_us": 0, "end_us": S, "mode": "loud"}]},
        {**good, "duration_us": 0},
        "nope",
    ):
        with pytest.raises((ValueError, KeyError, TypeError)):
            Timeline.from_dict(bad)


def test_project_stores_timelines_per_media(tmp_path):
    project = Project(name="p")
    t = Timeline.for_source(5 * S).split_at(2 * S).add_mute(S, 2 * S)
    project.set_timeline(r"C:\clips\A.mp4", t)
    assert project.timeline_for(r"c:\clips\a.mp4") == t  # case-insensitive lookup
    path = save_project(project, tmp_path / "p.tssproj")
    loaded = load_project(path)
    assert loaded.timeline_for(r"C:\clips\A.mp4") == t
    loaded.remove_media(r"C:\clips\A.mp4")
    assert not loaded.timelines

    (tmp_path / "bad.tssproj").write_text(
        '{"schema_version": 1, "timelines": [{"media": "a", "duration_us": 5}]}', encoding="utf-8"
    )
    with pytest.raises(ProjectFormatError):
        load_project(tmp_path / "bad.tssproj")


# ----------------------------------------------------------------- history

def test_history_undo_redo_and_merge():
    h = History(0)
    assert not h.record(0)
    h.record(1, now=0.0)
    h.record(2, now=10.0)
    assert h.undo() == 1 and h.undo() == 0 and h.undo() is None
    assert h.redo() == 1
    h.record(5, now=20.0)  # new edit clears redo
    assert not h.can_redo

    # A drag: many changes with the same key become one step.
    h = History("start")
    for i, state in enumerate(["a", "b", "c"]):
        h.record(state, key="drag", now=1.0 + i * 0.1)
    assert h.undo() == "start"
    h.redo()
    h.break_merge()
    h.record("d", key="drag", now=1.35)
    assert h.undo() == "c"

    h = History(0, limit=3)
    for i in range(1, 10):
        h.record(i, now=float(i))
    assert [h.undo() for _ in range(4)] == [8, 7, 6, None]
    h.reset(0)
    assert not h.can_undo and not h.can_redo


# ---------------------------------------------------------------- waveform

def test_peaks_and_columns():
    silence = np.zeros(8000, dtype=np.int16)
    loud = np.full(8000, 16384, dtype=np.int16)
    peaks = peaks_from_pcm(np.concatenate([silence, loud, -loud // 2]))
    assert len(peaks) == 300
    assert peaks[50] == 0 and peaks[150] == 128 and peaks[250] == 64

    w = Waveform(peaks)
    assert w.duration_s == pytest.approx(3.0)
    cols = w.columns(0.0, 3.0, 3)
    assert cols[0] == 0 and cols[1] == pytest.approx(128 / 255) and cols[2] == pytest.approx(64 / 255)
    # Zoomed far in (more pixels than peaks) and ranges beyond the audio.
    assert w.columns(1.2, 1.3, 500).min() == pytest.approx(128 / 255)
    beyond = w.columns(2.5, 4.0, 30)
    assert beyond[0] > 0 and beyond[-1] == 0
    assert w.columns(-2.0, -1.0, 10).max() == 0
    assert len(w.columns(0, 1, 0)) == 0
    assert Waveform(np.zeros(0, dtype=np.uint8)).columns(0, 1, 5).max() == 0
    # A single-sample spike is never lost when zoomed out.
    spike = np.zeros(1000, dtype=np.uint8)
    spike[777] = 200
    assert Waveform(spike).columns(0, 10, 7).max() == pytest.approx(200 / 255)
    assert Waveform(spike).columns(0, 10, 1000)[777] == pytest.approx(200 / 255)


def test_waveform_extraction_and_cache(sample_clips, ffmpeg_paths, tmp_path):
    clip = sample_clips["mp4"]
    before = clip.stat().st_mtime_ns
    w = load_waveform(clip, ffmpeg_paths.ffmpeg, tmp_path)
    assert w.duration_s == pytest.approx(2.0, abs=0.1)
    assert w.peaks.max() > 10  # the test tone is audible
    cached = cache_file(clip, tmp_path)
    assert cached.is_file()
    assert clip.stat().st_mtime_ns == before

    # Second call reads the cache (works even without FFmpeg).
    again = load_waveform(clip, tmp_path / "missing-ffmpeg", tmp_path)
    assert np.array_equal(again.peaks, w.peaks)

    # A damaged cache file is rebuilt.
    cached.write_bytes(b"garbage")
    assert np.array_equal(load_waveform(clip, ffmpeg_paths.ffmpeg, tmp_path).peaks, w.peaks)


def test_waveform_without_audio_raises(sample_clips, ffmpeg_paths, tmp_path):
    from core.audio.waveform import WaveformError

    with pytest.raises(WaveformError):
        load_waveform(sample_clips["mkv_noaudio"], ffmpeg_paths.ffmpeg, tmp_path)

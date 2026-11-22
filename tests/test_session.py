"""Autosave (recovery copies) and undo history kept between runs."""

from __future__ import annotations

import json

from core.censor.review import CensorState
from core.editor.composition import default_composition
from core.history import History
from core.projects.project import Project
from core.projects.session import (
    RecoveryStore,
    decode_histories,
    encode_histories,
    history_file,
    load_history,
    save_history,
)
from core.timeline.model import Timeline
from core.transcription.model import Segment, Transcript, Word, WORD_TIMING

S = 1_000_000


def _states():
    comp = default_composition(1280, 720)
    t0 = Timeline.for_source(4 * S)
    t1 = t0.split_at(2 * S)
    t2 = t1.with_gain_db(-3.0)
    words = (Word(0, 400_000, "привет", 0.9, WORD_TIMING),)
    tr = Transcript(language="ru", language_probability=0.9, model="small", segments=(Segment(1, 0, 400_000, "привет", words),), duration_us=4 * S)
    return [(comp, t0, None, None), (comp, t1, tr, None), (comp, t2, tr, CensorState())]


def test_history_snapshot_and_restore():
    h = History(1)
    for value in (2, 3, 4):
        h.record(value)
    h.undo()
    undo, current, redo = h.snapshot()
    assert (undo, current, redo) == ([1, 2], 3, [4])
    other = History(0)
    other.restore(undo, current, redo)
    assert other.current == 3 and other.undo() == 2 and other.redo() == 3 and other.redo() == 4


def test_histories_round_trip_and_share_parts():
    s0, s1, s2 = _states()
    data = encode_histories({"clip.mp4": ([s0, s1], s2, [])})
    # The composition and the transcript are stored once although several steps use them.
    assert len(data["pool"]) == 1 + 3 + 1 + 1
    restored = decode_histories(json.loads(json.dumps(data)))
    undo, current, redo = restored["clip.mp4"]
    assert undo == [s0, s1] and current == s2 and redo == []


def test_broken_history_is_ignored():
    assert decode_histories({"format": 99}) == {}
    data = encode_histories({"a": ([], _states()[0], [])})
    data["clips"].append({"media": "b", "current": ["missing", None, None, None]})
    assert set(decode_histories(data)) == {"a"}


def test_history_file_per_project(tmp_path):
    states = _states()
    path = save_history(tmp_path, tmp_path / "x.tssproj", {"clip": (states[:2], states[2], [])})
    assert path == history_file(tmp_path, tmp_path / "x.tssproj")
    assert load_history(tmp_path, tmp_path / "x.tssproj")["clip"][1] == states[2]
    assert load_history(tmp_path, tmp_path / "other.tssproj") == {}


def test_recovery_store(tmp_path):
    store = RecoveryStore(tmp_path)
    project = Project(name="clip one")
    key = store.key_for(None, "abc")
    assert key == "u-abc" and store.key_for(tmp_path / "p.tssproj", "abc").startswith("p-")
    states = _states()
    store.write(key, project.to_dict(), {"m": ([], states[1], [])}, None)
    entries = store.entries()
    assert [e.project_name for e in entries] == ["clip one"] and entries[0].original_path == ""
    data, histories = store.load(entries[0])
    assert Project.from_dict(data).name == "clip one" and histories["m"][1] == states[1]
    store.discard(key)
    assert store.entries() == []
    (store.folder / "junk.json").write_text("{not json", encoding="utf-8")
    assert store.entries() == []  # unreadable copies are skipped


def test_autosave_settings_in_config(data_dir):
    from app.config import AppConfig

    config = AppConfig.load(data_dir)
    assert config.autosave_enabled and config.autosave_minutes == 2
    config.autosave_enabled, config.autosave_minutes = False, 5
    config.save()
    loaded = AppConfig.load(data_dir)
    assert not loaded.autosave_enabled and loaded.autosave_minutes == 5


# ------------------------------------------------------------------ window

def _window(data_dir, ffmpeg_paths):
    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow

    return MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)


def test_undo_survives_switching_clips_and_reopening(qapp, data_dir, ffmpeg_paths, sample_clips, tmp_path):
    from tests.test_ui import _wait

    w = _window(data_dir, ffmpeg_paths)
    w.import_files([str(sample_clips["mp4"]), str(sample_clips["mkv_noaudio"])])
    assert _wait(lambda: w.media_panel.media_count() == 2 and w.timeline.timeline is not None)
    w.media_panel.select_media(sample_clips["mp4"])
    assert _wait(lambda: w.preview.media is not None and w.preview.media.path == sample_clips["mp4"])
    w.timeline.set_playhead(1 * S)
    w.act_split.trigger()
    assert len(w.timeline.timeline.segments) == 2 and w.act_undo.isEnabled()

    # Another clip and back: the split can still be undone.
    w.media_panel.select_media(sample_clips["mkv_noaudio"])
    assert _wait(lambda: w.preview.media.path == sample_clips["mkv_noaudio"])
    assert not w.act_undo.isEnabled()
    w.media_panel.select_media(sample_clips["mp4"])
    assert _wait(lambda: w.preview.media.path == sample_clips["mp4"])
    assert w.act_undo.isEnabled()

    path = tmp_path / "keep.tssproj"
    w.project_path = path
    assert w.save()
    w.pool.waitForDone(3000)
    w.close()

    other = _window(data_dir, ffmpeg_paths)
    other.open_project(path)
    assert _wait(lambda: other.media_panel.media_count() == 2)
    other.media_panel.select_media(sample_clips["mp4"])
    ok = _wait(lambda: other.preview.media is not None and other.preview.media.path == sample_clips["mp4"]
               and other.timeline.timeline is not None and len(other.timeline.timeline.segments) == 2)
    assert ok, (other.preview.media, other.timeline.timeline, list(other.project.timelines))
    assert other.act_undo.isEnabled() and not other._dirty
    other.act_undo.trigger()
    assert len(other.timeline.timeline.segments) == 1
    other.act_redo.trigger()
    assert len(other.timeline.timeline.segments) == 2
    other._set_dirty(False)
    other.close()


def test_recovery_copy_after_crash(qapp, data_dir, ffmpeg_paths, sample_clips):
    from tests.test_ui import _wait

    w = _window(data_dir, ffmpeg_paths)
    assert not w.session.autosave_now()  # nothing to save yet
    w.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: w.timeline.timeline is not None)
    w.timeline.set_playhead(1 * S)
    w.act_split.trigger()
    assert w.is_dirty and w.session.autosave_now()
    assert _wait(lambda: w.session.last_autosave > 0)
    assert len(w.session.store.entries()) == 1
    # "Crash": the window is dropped without closeEvent; the copy stays.
    w.session.timer.stop()
    w.hide()

    other = _window(data_dir, ffmpeg_paths)
    entries = other.session.entries()
    assert len(entries) == 1
    assert other.restore_recovery(entries[0])
    assert other.is_dirty and other.project_path is None
    assert _wait(lambda: other.timeline.timeline is not None and len(other.timeline.timeline.segments) == 2)
    assert other.act_undo.isEnabled()  # history came back too
    other.act_undo.trigger()
    assert len(other.timeline.timeline.segments) == 1

    # Saving removes the recovery copy.
    other.project_path = data_dir / "restored.tssproj"
    assert other.save()
    assert other.session.store.entries() == []
    other.close()


def test_discarding_changes_removes_recovery_copy(qapp, data_dir, ffmpeg_paths, sample_clips, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from tests.test_ui import _wait

    w = _window(data_dir, ffmpeg_paths)
    w.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: w.timeline.timeline is not None)
    w.act_split.trigger()
    assert w.session.autosave_now()
    assert _wait(lambda: w.session.last_autosave > 0)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    w.close()
    assert RecoveryStore(data_dir).entries() == []


def test_autosave_menu(qapp, data_dir, ffmpeg_paths):
    w = _window(data_dir, ffmpeg_paths)
    assert w.session.timer.isActive() and w.session.timer.interval() == 2 * 60_000
    w.autosave_actions[5].trigger()
    assert w.config.autosave_minutes == 5 and w.session.timer.interval() == 5 * 60_000
    w.act_autosave.setChecked(False)
    assert not w.session.timer.isActive() and not w.autosave_actions[5].isEnabled()
    w.close()

"""Timeline widget, edit actions, undo/redo and edited playback (offscreen)."""

from __future__ import annotations

import time
from fractions import Fraction

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.config import AppConfig
from core.audio.waveform import Waveform
from core.timeline.model import BEEP, SILENCE, Segment, Timeline
from ui.panels.timeline_panel import TimelinePanel
from ui.timeline.timeline_controller import MUTE, SEGMENT, TimelineController
from ui.timeline.timeline_view import AUDIO_Y, MUTE_Y, VIDEO_Y

S = 1_000_000


def drag(widget, start: QPointF, end: QPointF, modifiers=Qt.KeyboardModifier.NoModifier):
    a, b = start.toPoint(), end.toPoint()
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, modifiers, a)
    QTest.mouseMove(widget, QPoint((a.x() + b.x()) // 2, a.y()))
    QTest.mouseMove(widget, b)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, modifiers, b)
    QApplication.processEvents()


def click(widget, x, y):
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x, y))
    QApplication.processEvents()


@pytest.fixture
def controller(qapp):
    c = TimelineController()
    c.set_timeline(Timeline.for_source(10 * S))
    return c


@pytest.fixture
def view(controller):
    panel = TimelinePanel(controller)
    panel.resize(1020, 260)
    panel.show()
    QApplication.processEvents()
    v = panel.view
    v.set_waveform(Waveform(np.full(1000, 128, dtype=np.uint8)))
    v.set_zoom(100.0)  # 100 px per second → 1 px = 10 ms
    v.set_scroll_us(0)
    v._panel = panel  # keep the panel alive
    return v


VID = VIDEO_Y + 10
AUD = AUDIO_Y + 30
MUT = MUTE_Y + 10


def test_click_seeks_and_selects_segment(view, controller):
    seeks = []
    view.seek_requested.connect(seeks.append)
    click(view, 300, VID)
    assert seeks == [3 * S]
    assert controller.selected == (SEGMENT, 0)
    click(view, 450, 10)  # ruler
    assert seeks[-1] == 4_500_000


def test_drag_selects_range_then_mute(view, controller):
    drag(view, QPointF(200, AUD), QPointF(350, AUD))
    assert controller.range == (2 * S, 3_500_000)
    assert controller.mute_range()
    region = controller.timeline.mutes[0]
    assert (region.start_us, region.end_us, region.mode) == (2 * S, 3_500_000, SILENCE)
    assert controller.selected == (MUTE, region.id)
    assert controller.range is None

    controller.set_mute_mode(BEEP)
    assert controller.timeline.mutes[0].mode == BEEP
    assert controller.default_mute_mode == BEEP


def test_mute_region_move_and_resize(view, controller):
    controller.set_timeline(controller.timeline.add_mute(2 * S, 3 * S))
    QApplication.processEvents()
    drag(view, QPointF(250, MUT), QPointF(450, MUT))  # move by +2 s
    m = controller.timeline.mutes[0]
    assert (m.start_us, m.end_us) == (4 * S, 5 * S)
    drag(view, QPointF(500, MUT), QPointF(620, MUT), Qt.KeyboardModifier.AltModifier)  # right edge +1.2 s
    m = controller.timeline.mutes[0]
    assert (m.start_us, m.end_us) == (4 * S, 6_200_000)
    drag(view, QPointF(400, MUT), QPointF(350, MUT), Qt.KeyboardModifier.AltModifier)  # left edge −0.5 s
    assert controller.timeline.mutes[0].start_us == 3_500_000

    assert controller.delete_selection()
    assert controller.timeline.mutes == ()


def test_trim_edges_and_snap_to_playhead(view, controller):
    drag(view, QPointF(0, VID), QPointF(150, VID), Qt.KeyboardModifier.AltModifier)
    assert controller.timeline.segments[0].start_us == 1_500_000
    # End edge dragged near the playhead snaps onto it.
    controller.set_playhead(8 * S)
    drag(view, QPointF(1000, VID), QPointF(805, VID))
    assert controller.timeline.segments[0].end_us == 8 * S
    assert controller.timeline.output_us == 6_500_000


def test_trim_snaps_to_frames(view, controller):
    controller.fps = Fraction(30)
    drag(view, QPointF(1000, VID), QPointF(701, VID), Qt.KeyboardModifier.AltModifier)
    end = controller.timeline.segments[0].end_us
    assert end == round(round(end * 30 / S) * S / 30)  # on a frame boundary
    assert abs(end - 7_010_000) < 20_000


def test_split_point_edges_are_independent(view, controller):
    controller.set_playhead(5 * S)
    assert controller.split_at_playhead()
    QApplication.processEvents()
    # Grab just left of the split: moves the first segment's end.
    drag(view, QPointF(498, VID), QPointF(400, VID), Qt.KeyboardModifier.AltModifier)
    assert controller.timeline.segments == (Segment(0, 4 * S), Segment(5 * S, 10 * S))
    # Grab the second segment's start and pull it right.
    drag(view, QPointF(501, VID), QPointF(600, VID), Qt.KeyboardModifier.AltModifier)
    assert controller.timeline.segments[1].start_us == 6 * S


def test_delete_range_trim_and_restore(view, controller):
    controller.set_range(3 * S, 4 * S)
    assert controller.delete_selection()
    assert controller.timeline.output_us == 9 * S
    controller.set_range(1 * S, 2 * S)
    assert controller.trim_to_range()
    assert controller.timeline.segments == (Segment(1 * S, 2 * S),)
    assert controller.restore_all()
    assert controller.timeline.output_us == 10 * S

    controller.set_playhead(5 * S)
    controller.split_at_playhead()
    controller.select(SEGMENT, 0)
    assert controller.delete_selection()
    assert controller.timeline.segments == (Segment(5 * S, 10 * S),)
    assert controller.selected is None  # selection cleared when it became invalid


def test_zoom_scroll_and_fit(view, controller):
    QTest.qWait(1)
    view.set_zoom(400.0, anchor_x=500)  # 5 s stays under the cursor
    assert view.x_to_us(500) == pytest.approx(5 * S, abs=10_000)
    view.set_scroll_us(99 * S)  # clamped to the end
    assert view.us_to_x(10 * S) <= view.width()
    view.zoom_fit()
    assert view.scroll_us == 0
    assert view.us_to_x(10 * S) == pytest.approx(view.width() - 20, abs=1)
    panel = view._panel
    assert panel.scrollbar.maximum() == 0
    view.set_zoom(500.0)
    assert panel.scrollbar.maximum() > 0
    panel.scrollbar.setValue(panel.scrollbar.maximum())
    assert view.scroll_us > 0


def test_panel_audio_controls(view, controller):
    panel = view._panel
    panel.gain_spin.setValue(-6.0)
    assert controller.timeline.gain_db == -6.0
    panel.fade_in_spin.setValue(1.5)
    panel.fade_out_spin.setValue(0.5)
    assert (controller.timeline.fade_in_us, controller.timeline.fade_out_us) == (1_500_000, 500_000)
    controller.set_timeline(Timeline.for_source(10 * S).with_gain_db(3.0))
    assert panel.gain_spin.value() == 3.0
    assert "Output 00:10.000" in panel.info_label.text()
    # Painting with everything present must not raise.
    controller.set_timeline(
        controller.timeline.add_mute(S, 2 * S).add_mute(3 * S, 4 * S, BEEP).with_fades(S, S).delete_range(6 * S, 7 * S)
    )
    controller.set_range(4 * S, 5 * S)
    assert not view.grab().toImage().isNull()


# ---------------------------------------------------------- main window

def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def window(qapp, data_dir, ffmpeg_paths, sample_clips):
    from ui.main_window.main_window import MainWindow

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    w.resize(1400, 900)
    w.show()
    w.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: w.timeline.timeline is not None)
    yield w
    w._dirty = False
    w.close()


def test_window_builds_timeline_and_waveform(window):
    t = window.timeline.timeline
    assert t.duration_us == pytest.approx(2 * S, abs=100_000)
    assert window.timeline.fps == 30
    assert _wait(lambda: window.timeline_panel.view._waveform is not None)
    assert len(window.timeline_panel.view._waveform.peaks) > 150
    assert not window.act_undo.isEnabled() and not window.act_mute.isEnabled()
    assert not window._dirty or window.project.composition is not None


def test_undo_redo_across_timeline_and_composition(window):
    original = window.timeline.timeline
    window._set_dirty(False)
    window.timeline.set_range(500_000, 1_000_000)
    assert window.act_mute.isEnabled()
    window.act_mute.trigger()
    assert len(window.timeline.timeline.mutes) == 1
    assert window._dirty

    window.history.break_merge()
    window.composition.set_divider(0.5)
    divider_before = 0.35
    assert window.act_undo.isEnabled()

    window.act_undo.trigger()  # undo divider
    assert window.composition.composition.divider == pytest.approx(divider_before)
    assert len(window.timeline.timeline.mutes) == 1
    window.act_undo.trigger()  # undo mute
    assert window.timeline.timeline == original
    assert not window.act_undo.isEnabled()

    window.act_redo.trigger()
    window.act_redo.trigger()
    assert len(window.timeline.timeline.mutes) == 1
    assert window.composition.composition.divider == pytest.approx(0.5)
    assert not window.act_redo.isEnabled()
    # The project always mirrors the current state.
    path = str(window.preview.media.path)
    assert window.project.timeline_for(path) == window.timeline.timeline


def test_drag_is_one_undo_step(window):
    view = window.timeline_panel.view
    QApplication.processEvents()
    start_us = window.timeline.timeline.segments[0].start_us
    x0 = view.us_to_x(0)
    drag(view, QPointF(x0, VID), QPointF(x0 + 200, VID), Qt.KeyboardModifier.AltModifier)
    assert window.timeline.timeline.segments[0].start_us > start_us
    window.act_undo.trigger()
    assert window.timeline.timeline.segments[0].start_us == start_us
    assert not window.act_undo.isEnabled()


def test_timeline_saved_and_restored(window, tmp_path, data_dir, ffmpeg_paths):
    from ui.main_window.main_window import MainWindow

    window.timeline.set_range(200_000, 600_000)
    window.act_mute.trigger()
    window.timeline.set_playhead(1 * S)
    window.act_split.trigger()
    window.timeline.set_gain_db(-3.0)
    saved = window.timeline.timeline
    window.project_path = tmp_path / "edit.tssproj"
    assert window.save()

    other = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    other.open_project(tmp_path / "edit.tssproj")
    assert _wait(lambda: other.timeline.timeline is not None)
    assert other.timeline.timeline == saved
    assert not other._dirty and not other.act_undo.isEnabled()
    other.close()


def test_playback_skips_cut_and_mutes(window):
    from PySide6.QtMultimedia import QMediaPlayer

    player = window.preview.player
    assert _wait(lambda: player.duration() > 0)
    # Keep 0.2–0.6 s and 1.2–1.6 s, with a mute at 1.3–1.5 s.
    t = window.timeline.timeline.keep_range(200_000, 1_600_000).delete_range(600_000, 1_200_000)
    window.timeline.set_timeline(t.add_mute(1_300_000, 1_500_000))

    seen: list[tuple[int, float]] = []
    player.positionChanged.connect(lambda pos: seen.append((pos, window.preview._audio_factor)))
    player.setPosition(0)
    player.play()
    assert _wait(lambda: player.playbackState() != QMediaPlayer.PlaybackState.PlayingState and len(seen) > 5, 8.0)

    positions = [p for p, _ in seen]
    assert max(positions) <= 1700  # stopped at the end of the edit
    # No time was spent inside the removed middle part (small decode tolerance).
    assert not [p for p in positions if 700 <= p <= 1150]
    assert [p for p in positions if 1200 <= p <= 1600]
    # Volume is zero inside the mute region and full outside it.
    assert window.timeline.timeline.volume_at(1_400_000) == 0
    muted = [f for p, f in seen if 1340 <= p <= 1460]
    assert muted and max(muted) == 0
    window.preview.seek(400)
    window.edit_playback.tick()
    assert window.preview._audio_factor == 1.0

"""Each part of the clip can have its own Stacked layout (the webcam moved)."""

from __future__ import annotations

import subprocess
import time
from dataclasses import replace

import numpy as np
import pytest

from core.editor.composition import Region, default_composition
from core.editor.geometry import Rect
from core.timeline.model import Timeline

S = 1_000_000


def _webcam_at(comp, x: float):
    return comp.with_region("webcam", Region(Rect(x, 0.0, 0.25, 0.4)))


def test_parts_keep_their_layout_through_edits():
    main = default_composition(1280, 720)
    other = _webcam_at(main, 0.7)
    t = Timeline.for_source(10 * S).split_at(4 * S).with_segment_layout(1, other)
    assert t.has_layouts and t.segments[0].layout is None and t.segments[1].layout == other
    split = t.split_at(7 * S)
    assert split.segments[1].layout == other and split.segments[2].layout == other
    trimmed = split.trim_segment(2, end_us=9 * S).delete_range(5 * S, 6 * S)
    assert all(s.layout == other for s in trimmed.segments[1:])
    assert t.layout_index_at(3 * S) == 0 and t.layout_index_at(5 * S) == 1
    gap = t.delete_range(3 * S, 5 * S)
    assert gap.layout_index_at(4 * S) == 1  # in a cut: the next part's layout
    assert Timeline.from_dict(t.to_dict()) == t
    assert Timeline.from_dict(Timeline.for_source(S).to_dict()).segments[0].layout is None
    assert not t.without_layouts().has_layouts and t.with_segment_layout(9, other) is t


@pytest.fixture
def two_colour_clip(tmp_path, ffmpeg_paths):
    """Left half red, right half blue, 2 s with audio."""
    path = tmp_path / "halves.mp4"
    subprocess.run([
        str(ffmpeg_paths.ffmpeg), "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=640x360:d=2:r=30",
        "-f", "lavfi", "-i", "color=c=blue:s=320x360:d=2:r=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-filter_complex", "[0][1]overlay=x=320[v]", "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def test_render_switches_layout_at_the_cut(two_colour_clip, ffmpeg_paths, tmp_path):
    from core.media.probe import probe_media
    from core.render.graph import build_graph
    from core.render.job import layout_divider_at, render
    from core.render.settings import RenderSettings

    info = probe_media(two_colour_clip, ffmpeg_paths.ffprobe)
    main = _webcam_at(default_composition(640, 360), 0.0)  # webcam = left (red)
    right = replace(_webcam_at(main, 0.7), divider=0.5)  # webcam = right (blue)
    timeline = Timeline.for_source(round(info.duration_s * S)).split_at(1 * S).with_segment_layout(1, right)
    graph = build_graph(main, timeline, info, RenderSettings(width=270, height=480, fps=30))
    assert "[canvas0]" in graph.filter_complex and "[canvas1]" in graph.filter_complex
    assert layout_divider_at(main, timeline)(1_500_000) == 0.5
    assert layout_divider_at(main, timeline)(500_000) == main.divider
    assert layout_divider_at(main, Timeline.for_source(S)) is None

    out = render(info, main, timeline, RenderSettings(width=270, height=480, fps=30), tmp_path / "parts.mp4",
                 ffmpeg_paths.ffmpeg, ffmpeg_paths.ffprobe, None)

    def top_colour(at: float) -> np.ndarray:
        raw = subprocess.run(
            [str(ffmpeg_paths.ffmpeg), "-v", "error", "-ss", f"{at}", "-i", str(out.path), "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(480, 270, 3).astype(int)
        return frame[40:120, 60:210].reshape(-1, 3).mean(axis=0)

    first, second = top_colour(0.5), top_colour(1.5)
    assert first[0] > 150 and first[2] < 100  # red webcam in part 1
    assert second[2] > 150 and second[0] < 100  # blue webcam in part 2


def _wait(pred, timeout=10.0):
    from PySide6.QtWidgets import QApplication

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_window_edits_the_part_under_the_playhead(qapp, data_dir, ffmpeg_paths, sample_clips, tmp_path):
    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    w.show()
    w.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: w.timeline.timeline is not None)
    panel = w.composition_panel
    main = w.main_layout
    assert panel.part_card.isHidden()

    # One part: edits change the project's layout, as before.
    w.composition.set_divider(0.4)
    assert w.main_layout.divider == 0.4 and not w.timeline.timeline.has_layouts
    main = w.main_layout

    # Split at 1 s, go to part 2 and change the layout there only.
    w.timeline.set_playhead(1 * S)
    w.act_split.trigger()
    assert len(w.timeline.timeline.segments) == 2
    w.timeline.set_playhead(1_500_000)
    assert not panel.part_card.isHidden() and "part 2 of 2" in panel.part_label.text()
    w.composition.set_divider(0.6)
    t = w.timeline.timeline
    assert t.segments[1].layout.divider == 0.6 and t.segments[0].layout is None
    assert w.main_layout == main  # the common layout is untouched
    assert "own layout" in panel.part_label.text() and panel.apply_all_button.isEnabled()

    # Back in part 1 the editor shows the common layout again.
    w.timeline.set_playhead(500_000)
    assert w.composition.composition.divider == 0.4
    w.timeline.set_playhead(1_500_000)
    assert w.composition.composition.divider == 0.6

    # Undo takes the part's layout away again; redo brings it back.
    w.act_undo.trigger()
    assert not w.timeline.timeline.has_layouts and w.composition.composition.divider == 0.4
    w.act_redo.trigger()
    assert w.timeline.timeline.segments[1].layout.divider == 0.6 and w.composition.composition.divider == 0.6

    # Saved with the project.
    w.project_path = tmp_path / "parts.tssproj"
    assert w.save()
    other = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    other.open_project(tmp_path / "parts.tssproj")
    assert _wait(lambda: other.timeline.timeline is not None and other.timeline.timeline.has_layouts)
    assert other.timeline.timeline.segments[1].layout.divider == 0.6 and other.main_layout.divider == 0.4
    other._dirty = False
    other.close()

    # "Use this layout for all parts" makes the shown layout the common one.
    panel.apply_all_button.click()
    assert not w.timeline.timeline.has_layouts and w.main_layout.divider == 0.6
    w.timeline.set_playhead(500_000)
    assert w.composition.composition.divider == 0.6
    w._dirty = False
    w.close()

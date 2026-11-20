"""Stage 7: keyframed zoom and pan of a region (mostly gameplay)."""

from __future__ import annotations

import subprocess

import numpy as np
import pytest

from core.editor.composition import GAMEPLAY, Composition, Region, default_composition
from core.editor.geometry import Rect
from core.editor.track import MAX_ZOOM, TrackKey, clean_track, has_zoom, zoom_at
from core.media.probe import probe_media
from core.render.graph import build_graph
from core.render.settings import RenderSettings
from core.timeline.model import Timeline

S = 1_000_000


def _zoomed() -> Composition:
    comp = default_composition(1280, 720)
    keys = (TrackKey(0, 0.0, 0.0, 1.0), TrackKey(2 * S, 0.0, 0.0, 2.0))
    return comp.with_region(GAMEPLAY, Region(Rect(0.2, 0.2, 0.6, 0.6), track=keys))


def test_zoom_key_round_trip_and_clamp():
    key = TrackKey(S, 0.1, 0.0, 1.5)
    assert TrackKey.from_list(key.to_list()) == key
    assert len(TrackKey(S, 0.1, 0.0).to_list()) == 3  # old files stay the same
    assert TrackKey.from_list([S, 0.1, 0.0]).z == 1.0
    keys = clean_track([TrackKey(0), TrackKey(S, 0, 0, 9.0)])
    assert keys[-1].z == MAX_ZOOM and has_zoom(keys)
    assert clean_track([TrackKey(0), TrackKey(S)]) == ()


def test_zoom_interpolates_and_shrinks_crop_around_centre():
    comp = _zoomed()
    keys = comp.gameplay.track
    assert zoom_at(keys, -S) == 1.0 and zoom_at(keys, S) == pytest.approx(1.5) and zoom_at(keys, 5 * S) == 2.0
    crop = comp.at(2 * S).gameplay.crop
    assert crop.w == pytest.approx(0.3) and crop.h == pytest.approx(0.3)
    assert crop.x + crop.w / 2 == pytest.approx(0.5) and crop.y + crop.h / 2 == pytest.approx(0.5)
    assert Composition.from_dict(comp.to_dict()) == comp


def test_graph_scales_frame_for_zoom(sample_clips, ffmpeg_paths):
    info = probe_media(sample_clips["mp4"], ffmpeg_paths.ffprobe)
    graph = build_graph(_zoomed(), Timeline.for_source(2 * S), info, RenderSettings(width=540, height=960))
    assert "eval=frame" in graph.filter_complex


def test_render_zooms_in(sample_clips, ffmpeg_paths, tmp_path):
    """The gameplay slot shows a different picture once the zoom has grown."""
    from core.render.job import render

    info = probe_media(sample_clips["mp4"], ffmpeg_paths.ffprobe)
    comp = default_composition(1280, 720).with_region(
        GAMEPLAY, Region(Rect(0.0, 0.0, 1.0, 1.0), track=(TrackKey(0), TrackKey(2 * S, 0.0, 0.0, 3.0))))
    result = render(info, comp, Timeline.for_source(2 * S), RenderSettings(width=360, height=640, loudness_lufs=0),
                    tmp_path / "z.mp4", ffmpeg_paths.ffmpeg, ffmpeg_paths.ffprobe)
    assert result.path.is_file()

    def frame(at: float) -> np.ndarray:
        raw = subprocess.run([str(ffmpeg_paths.ffmpeg), "-loglevel", "error", "-ss", str(at), "-i", str(result.path),
                              "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                             capture_output=True, check=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(640, 360)

    a, b = frame(0.0).astype(int), frame(1.9).astype(int)
    assert np.abs(a - b).mean() > 5


def test_controller_keys(qapp):
    from ui.editor.composition_controller import CompositionController

    c = CompositionController()
    c.set_source_size(1000, 1000)
    c.update_region(GAMEPLAY, crop=Rect(0.2, 0.2, 0.6, 0.6))
    c.add_key(GAMEPLAY)  # neutral first key: kept as pending, no track yet
    assert c.keys(GAMEPLAY) and not c.composition.gameplay.track
    c.set_time(2 * S)
    c.add_key(GAMEPLAY, zoom=2.0)
    assert len(c.composition.gameplay.track) == 2 and c.zoom(GAMEPLAY) == pytest.approx(2.0)
    # Dragging the crop at a key pans that key.
    shown = c.crop_px(GAMEPLAY)
    c.set_crop_px(GAMEPLAY, Rect(shown.x + 100, shown.y, shown.w, shown.h))
    assert c.key_at(GAMEPLAY).dx == pytest.approx(0.1)
    assert c.composition.gameplay.crop == Rect(0.2, 0.2, 0.6, 0.6)  # base crop untouched
    assert c.remove_key(GAMEPLAY) and not c.composition.gameplay.track
    c.set_time(0)
    assert c.remove_key(GAMEPLAY) and not c.keys(GAMEPLAY)
    c.set_time(S)
    c.add_key(GAMEPLAY, zoom=1.5)
    c.clear_track(GAMEPLAY)
    assert not c.keys(GAMEPLAY) and c.composition.gameplay.crop == Rect(0.2, 0.2, 0.6, 0.6)


def test_panel_zoom_card(qapp, tmp_path):
    from core.editor.presets import PresetStore
    from ui.editor.composition_controller import CompositionController
    from ui.panels.composition_panel import CompositionPanel

    c = CompositionController()
    panel = CompositionPanel(c, PresetStore(tmp_path / "p.json"))
    c.select(GAMEPLAY)
    assert not panel.remove_key_button.isEnabled()
    panel.add_key_button.click()
    assert panel.remove_key_button.isEnabled() and not panel.add_key_button.isEnabled()
    c.set_time(2 * S)
    panel.zoom_slider.setValue(200)
    assert c.composition.gameplay.track and c.zoom(GAMEPLAY) == pytest.approx(2.0)
    assert panel.zoom_label.text() == "200 %"
    c.set_time(S)
    assert panel.zoom_slider.value() == 150
    panel.clear_keys_button.click()
    assert not c.keys(GAMEPLAY) and panel.zoom_status.text() == "No keys yet."

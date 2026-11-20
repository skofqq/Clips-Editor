"""Webcam auto-framing: tracks on crops, render expressions, face tracking.

The tests that run the real face model need it on disk: set TSS_FACE_MODEL
to the path of version-RFB-320.onnx (the app downloads it on first use)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from core.editor.composition import WEBCAM, Composition, Region, default_composition
from core.editor.geometry import Rect
from core.editor.presets import PresetStore
from core.editor.track import TrackKey, clean_track, offset_at, piecewise_expression
from core.media.probe import probe_media
from core.render.graph import build_graph
from core.render.settings import RenderSettings
from core.timeline.model import Timeline
from core.vision.autoframe import Sample, choose_face, frame_crop
from core.vision.faces import Face, nms

S = 1_000_000
FACE_MODEL = os.environ.get("TSS_FACE_MODEL", "")
needs_model = pytest.mark.skipif(not (FACE_MODEL and Path(FACE_MODEL).is_file()),
                                 reason="set TSS_FACE_MODEL to the face model to run")


def _tracked() -> Composition:
    comp = default_composition(1280, 720)
    keys = (TrackKey(0, 0.0, 0.0), TrackKey(2 * S, 0.1, 0.0), TrackKey(4 * S, 0.1, 0.05))
    return comp.with_region(WEBCAM, Region(Rect(0.5, 0.5, 0.2, 0.2), track=keys))


# ------------------------------------------------------------------ track model

def test_offsets_interpolate_and_hold():
    keys = clean_track([TrackKey(2 * S, 0.2, 0.0), TrackKey(0, 0.0, 0.0), TrackKey(2 * S, 0.1, 0.0)])
    assert keys == (TrackKey(0, 0.0, 0.0), TrackKey(2 * S, 0.1, 0.0))  # one key per time, sorted
    assert offset_at(keys, -5) == (0.0, 0.0)
    assert offset_at(keys, S) == pytest.approx((0.05, 0.0))
    assert offset_at(keys, 9 * S) == (0.1, 0.0)
    assert clean_track([TrackKey(0), TrackKey(S)]) == ()  # no motion = no track


def test_composition_at_time_and_round_trip():
    comp = _tracked()
    assert comp.has_tracks and comp.at(0).webcam.crop.x == pytest.approx(0.5)
    assert comp.at(S).webcam.crop.x == pytest.approx(0.55)
    assert comp.at(4 * S).webcam.crop.y == pytest.approx(0.55) and not comp.at(S).has_tracks
    # A moved crop never leaves the frame.
    far = comp.with_region(WEBCAM, Region(Rect(0.85, 0.5, 0.2, 0.2), track=comp.webcam.track))
    assert far.at(4 * S).webcam.crop.right <= 1.0
    assert Composition.from_dict(comp.to_dict()) == comp
    assert "track" not in default_composition().to_dict()["webcam"]
    assert not comp.without_tracks().has_tracks


def test_presets_drop_tracks(tmp_path):
    store = PresetStore(tmp_path / "p.json")
    store.save("mine", _tracked())
    assert not PresetStore(tmp_path / "p.json").get("mine").composition.has_tracks


def test_piecewise_expression_values():
    expr = piecewise_expression([(0.0, 10.0), (2.0, 30.0), (4.0, 30.0)])

    def ev(t: float) -> float:  # evaluate the FFmpeg expression in Python
        code = expr.replace("if(", "_if(").replace("lt(", "_lt(")
        return eval(code, {"_if": lambda c, a, b: a if c else b, "_lt": lambda a, b: a < b, "t": t})

    assert ev(-1) == 10 and ev(1) == pytest.approx(20) and ev(3) == 30 and ev(10) == 30
    assert piecewise_expression([(0.0, 5.0), (1.0, 5.0)]) == "5.000"


def test_controller_keeps_motion_when_crop_is_dragged(qapp):
    from ui.editor.composition_controller import CompositionController

    c = CompositionController()
    c.set_source_size(1000, 1000)
    c.set_composition(_tracked())
    c.set_time(2 * S)
    shown = c.crop_px(WEBCAM)
    assert shown.x == pytest.approx(600)  # 0.5 + 0.1 track offset
    c.set_crop_px(WEBCAM, Rect(shown.x + 50, shown.y, shown.w, shown.h))
    assert c.crop_px(WEBCAM).x == pytest.approx(650)
    assert c.composition.webcam.crop.x == pytest.approx(0.55) and c.composition.webcam.track
    c.clear_track(WEBCAM)
    assert not c.composition.webcam.track and c.composition.webcam.crop.x == pytest.approx(0.65)


def test_graph_moves_tracked_crop(sample_clips, ffmpeg_paths):
    info = probe_media(sample_clips["mp4"], ffmpeg_paths.ffprobe)
    t = Timeline.for_source(2 * S).delete_range(500_000, 1_000_000)
    graph = build_graph(_tracked(), t, info, RenderSettings(width=540, height=960))
    assert "crop=w=" in graph.filter_complex and "x='clip(if(lt(t," in graph.filter_complex
    # The key at source 2 s lands at output 1.5 s (0.5 s was cut before it).
    assert "1.5000" in graph.filter_complex
    plain = build_graph(default_composition(1280, 720), t, info, RenderSettings())
    assert "x='" not in plain.filter_complex


def test_render_follows_the_track(sample_clips, ffmpeg_paths, tmp_path):
    """Render a tracked webcam and check the crop really moved: the left
    edge of the webcam slot shows different source columns at 0 s and 1.9 s."""
    from core.render.job import render

    info = probe_media(sample_clips["mp4"], ffmpeg_paths.ffprobe)
    comp = default_composition(1280, 720).with_region(
        WEBCAM, Region(Rect(0.0, 0.0, 0.3, 0.3), track=(TrackKey(0, 0.0, 0.0), TrackKey(2 * S, 0.5, 0.0))))
    result = render(info, comp, Timeline.for_source(2 * S), RenderSettings(width=360, height=640, loudness_lufs=0),
                    tmp_path / "t.mp4", ffmpeg_paths.ffmpeg, ffmpeg_paths.ffprobe)

    def frame(at: float) -> np.ndarray:
        raw = subprocess.run([str(ffmpeg_paths.ffmpeg), "-loglevel", "error", "-ss", str(at), "-i", str(result.path),
                              "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                             capture_output=True, check=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(640, 360)

    top_start, top_end = frame(0.0)[:150], frame(1.9)[:150]
    assert np.abs(top_start.astype(int) - top_end.astype(int)).mean() > 10


# ------------------------------------------------------------------ framing logic

def test_nms_and_choose_face():
    boxes = np.array([[0, 0, 1, 1], [0.05, 0, 1, 1], [2, 2, 3, 3]], dtype=np.float32)
    assert nms(boxes, np.array([0.9, 0.8, 0.7])) == [0, 2]
    streamer = [Face(0.80, 0.70, 0.84, 0.76, 0.99)]
    poster = [Face(0.20, 0.20, 0.23, 0.25, 0.9)]
    found = [streamer, streamer + poster, streamer, poster]
    assert choose_face(found).cx == pytest.approx(0.82)
    # Inside the current webcam crop wins even when seen less often.
    assert choose_face([poster, poster, streamer], prefer=Rect(0.7, 0.6, 0.3, 0.4)).cx == pytest.approx(0.82)
    assert choose_face([[], []]) is None


def test_frame_crop_static_and_following():
    def face(cx: float) -> Face:
        return Face(cx - 0.02, 0.60, cx + 0.02, 0.68, 0.99)

    still = [Sample(i * 500_000, face(0.8)) for i in range(10)]
    result = frame_crop(still, 1920, 1080, 1080 / 672, follow=True)
    crop = result.crop
    assert result.track == () and result.found == 10
    assert crop.h * 1080 == pytest.approx(0.08 * 1080 * 2.6, rel=0.01)  # head and shoulders
    assert (crop.w * 1920) / (crop.h * 1080) == pytest.approx(1080 / 672, rel=0.01)  # slot shape
    assert crop.x < 0.8 < crop.right and crop.y < 0.64 < crop.bottom

    # The streamer leans right half way through (one sample is missed).
    moving = [Sample(i * 500_000, face(0.7 if i < 10 else 0.8)) for i in range(20)]
    moving[5] = Sample(moving[5].t_us, None)
    result = frame_crop(moving, 1920, 1080, 1080 / 672, follow=True)
    assert result.track and result.found == 19
    start, end = offset_at(result.track, 0), offset_at(result.track, 10 * S)
    assert end[0] - start[0] == pytest.approx(0.1, abs=0.02) and len(result.track) <= 6
    assert frame_crop(moving, 1920, 1080, 1080 / 672, follow=False).track == ()
    assert frame_crop([Sample(0, None)], 1920, 1080, 1.6) is None


# ------------------------------------------------------------------ real model

@pytest.fixture(scope="module")
def face_video(tmp_path_factory, ffmpeg_paths) -> Path:
    """A 1280×720 'stream': gameplay test pattern and a face in the bottom-right
    webcam corner that jumps 120 px to the right after 3 seconds."""
    path = tmp_path_factory.mktemp("face") / "stream.mp4"
    face = Path(__file__).parent / "data" / "face.jpg"
    subprocess.run([
        str(ffmpeg_paths.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=6", "-loop", "1", "-i", str(face),
        "-filter_complex", "[1]scale=200:200[f];[0][f]overlay=x='900+if(gt(t,3),120,0)':y=420:shortest=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ], check=True)
    return path


@needs_model
def test_detector_finds_small_webcam_face():
    from core.vision.faces import FaceDetector

    img = np.array(__import__("PIL.Image", fromlist=["Image"]).open(Path(__file__).parent / "data" / "face.jpg"))
    frame = np.full((540, 960, 3), 90, np.uint8)
    frame[380:500, 800:920] = _shrink(img, 120)
    faces = FaceDetector(Path(FACE_MODEL)).detect(frame)
    assert faces and 0.83 < faces[0].cx < 0.96 and 0.7 < faces[0].cy < 0.93


def _shrink(img: np.ndarray, size: int) -> np.ndarray:
    from core.vision.faces import _resize

    return _resize(img, size, size)


@needs_model
def test_autoframe_follows_face(face_video, ffmpeg_paths):
    from core.vision.autoframe import autoframe
    from core.vision.faces import FaceDetector

    result = autoframe(ffmpeg_paths.ffmpeg, face_video, lambda: FaceDetector(Path(FACE_MODEL)), 0, 6 * S,
                       1280, 720, 1080 / 672, follow=True)
    assert result.found >= result.total * 0.8
    assert 0.6 < result.crop.x + result.crop.w / 2 < 0.95  # around the face, right side
    dx = (offset_at(result.track, 6 * S)[0] - offset_at(result.track, 0)[0]) * 1280
    assert 60 < dx < 160  # followed the 120 px jump


@needs_model
def test_window_auto_frame(qapp, data_dir, ffmpeg_paths, face_video):
    from app.config import AppConfig
    from core.vision.faces import FaceDetector
    from tests.test_ui import _wait
    from ui.main_window.main_window import MainWindow

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    w.import_files([str(face_video)])
    assert _wait(lambda: w.timeline.timeline is not None)
    w.autoframe.detector_factory = lambda: FaceDetector(Path(FACE_MODEL))
    w.composition_panel.autoframe_button.click()
    assert _wait(lambda: w.autoframe.task is None, 60)
    webcam = w.composition.composition.webcam
    assert webcam.track and webcam.crop.x > 0.5
    assert "Face found" in w.composition_panel.face_status.text()
    assert w.act_undo.isEnabled()
    w.act_undo.trigger()
    assert not w.composition.composition.webcam.track
    w._set_dirty(False)
    w.close()

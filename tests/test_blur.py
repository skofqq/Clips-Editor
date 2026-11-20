"""Blur boxes: model, timeline track, preview painting, object following, render."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from core.editor.blur import PIXELATE, BlurRegion
from core.editor.geometry import Rect
from core.editor.track import TrackKey, offset_at
from core.timeline.model import Timeline
from tests.test_editor_ui import drag

S = 1_000_000


def _checker(w=640, h=360) -> QImage:
    image = QImage(w, h, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    for y in range(0, h, 8):
        for x in range(0, w, 8):
            painter.fillRect(x, y, 8, 8, QColor("#ffffff" if (x // 8 + y // 8) % 2 else "#000000"))
    painter.end()
    return image


def _gray(image: QImage) -> np.ndarray:
    image = image.convertToFormat(QImage.Format.Format_Grayscale8)
    data = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
    return data[:, : image.width()].astype(int)


# ------------------------------------------------------------------ model
def test_blur_model_and_timeline_roundtrip():
    t = Timeline.for_source(10 * S).add_mute(0, S)
    t = t.add_blur(2 * S, 4 * S, Rect(0.1, 0.2, 0.3, 0.2), strength=80, mode=PIXELATE)
    blur = t.blurs[0]
    assert blur.id == 2 and t.next_id == 3 and blur.mode == PIXELATE and blur.strength == 80
    assert blur.active(2 * S) and not blur.active(4 * S)
    t = t.update_blur(blur.id, track=(TrackKey(2 * S, 0, 0), TrackKey(4 * S, 0.2, 0.1)))
    moved = t.blur(blur.id).rect_at(3 * S)
    assert moved.x == pytest.approx(0.2) and moved.y == pytest.approx(0.25)
    assert t.blur(blur.id).rect_at(9 * S).x == pytest.approx(0.3)  # held after the last key
    again = Timeline.from_dict(t.to_dict())
    assert again == t and again.next_id == 3

    # Moving in time takes the motion path along; edges are clamped.
    shifted = t.move_blur(blur.id, 5 * S).blur(blur.id)
    assert (shifted.start_us, shifted.end_us) == (5 * S, 7 * S)
    assert shifted.track[0].t_us == 5 * S and shifted.rect_at(6 * S).x == pytest.approx(0.2)
    assert t.update_blur(blur.id, end_us=99 * S).blur(blur.id).end_us == 10 * S
    assert t.update_blur(blur.id, start_us=4 * S).blur(blur.id).start_us == 4 * S - 100_000
    # Out-of-frame boxes are pulled inside, values clamped.
    odd = BlurRegion(1, 0, S, Rect(0.9, -0.2, 0.5, 0.5), strength=500, feather=-3).cleaned(10 * S)
    assert odd.rect.x == pytest.approx(0.5) and odd.rect.y == 0 and odd.strength == 100 and odd.feather == 0
    assert t.delete_blur(blur.id).blurs == ()
    # Old projects without blurs still load.
    data = t.to_dict()
    data.pop("blurs")
    assert Timeline.from_dict(data).blurs == ()


# --------------------------------------------------------------- painting
def test_apply_blurs_only_inside_the_box_and_feathered():
    from core.editor.blur_image import apply_blurs

    image = _checker()
    sharp = BlurRegion(1, 0, S, Rect(0.25, 0.25, 0.5, 0.5), strength=60, feather=0)
    soft = BlurRegion(1, 0, S, Rect(0.25, 0.25, 0.5, 0.5), strength=60, feather=100)
    before = _gray(image)
    after = _gray(apply_blurs(image, (sharp,), 0, 1280, 720))
    inside = after[120:240, 200:440]
    assert inside.std() < before[120:240, 200:440].std() / 4  # the checker is smoothed out
    assert (after[:80] == before[:80]).all() and (after[:, :150] == before[:, :150]).all()
    # Inactive at this time: untouched.
    assert (_gray(apply_blurs(image, (sharp,), 2 * S, 1280, 720)) == before).all()
    # Soft edges: just inside the border the picture is still almost sharp.
    edge = _gray(apply_blurs(image, (soft,), 0, 1280, 720))[95:100, 200:440]
    assert edge.std() > inside.std() * 3
    pixel = _gray(apply_blurs(image, (BlurRegion(1, 0, S, Rect(0.25, 0.25, 0.5, 0.5), strength=80,
                                                    feather=0, mode=PIXELATE),), 0, 1280, 720))
    block = pixel[120:240, 200:440]
    assert len(np.unique(block)) < len(np.unique(before[120:240, 200:440])) + 30


# ---------------------------------------------------------------- tracking
@pytest.fixture(scope="module")
def moving_clip(tmp_path_factory, ffmpeg_paths) -> Path:
    path = tmp_path_factory.mktemp("blur") / "moving.mp4"
    subprocess.run([
        str(ffmpeg_paths.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=gray:s=640x360:d=3:r=30",
        "-f", "lavfi", "-i", "testsrc2=s=80x60:d=3:r=30",
        "-filter_complex", "[0][1]overlay=x='50+100*t':y='60+40*t'",
        "-pix_fmt", "yuv420p", str(path),
    ], check=True)
    return path


def test_object_is_followed_forwards_and_backwards(moving_clip, ffmpeg_paths):
    from core.vision.object_track import match, track_object

    # The object is at (200, 120) at 1.5 s.
    rect = Rect(200 / 640, 120 / 360, 80 / 640, 60 / 360)
    keys = track_object(ffmpeg_paths.ffmpeg, moving_clip, 640, 360, rect, 1_500_000, 0, 3 * S, rect)
    assert 2 <= len(keys) <= 20
    for t_us in (0, 700_000, 2_400_000):
        dx, dy = offset_at(keys, t_us)
        assert (rect.x + dx) * 640 == pytest.approx(50 + 100 * t_us / S, abs=4)
        assert (rect.y + dy) * 360 == pytest.approx(60 + 40 * t_us / S, abs=4)
    # A flat template has nothing to follow.
    assert match(np.zeros((50, 50), np.float32), np.ones((10, 10), np.float32))[2] == 0.0


# ------------------------------------------------------------------ render
def test_render_blurs_only_the_moving_box(ffmpeg_paths, moving_clip, tmp_path):
    from core.render.graph import blur_lines

    box = BlurRegion(1, 500_000, 2_500_000, Rect(100 / 640, 80 / 360, 80 / 640, 60 / 360), strength=60,
                     feather=30, track=(TrackKey(500_000, 0, 0), TrackKey(2_500_000, 200 / 640, 80 / 360)))
    graph = ";".join(["[0:v]null[src]"] + blur_lines((box,), 640, 360, "src", "out"))

    def frames(*extra):
        raw = subprocess.run([str(ffmpeg_paths.ffmpeg), "-loglevel", "error", "-i", str(moving_clip), *extra,
                              "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(-1, 360, 640).astype(int)

    plain, blurred = frames(), frames("-filter_complex", graph, "-map", "[out]")
    diff = np.abs(plain - blurred)
    assert diff[5].max() < 12  # 0.17 s: before the blur starts
    # 1.5 s: the box moved half way, to (200, 120); the object is there too.
    inside = diff[45, 125:165, 210:270]
    outside = np.concatenate([diff[45, :100].ravel(), diff[45, 200:].ravel()])
    assert inside.mean() > 8 and outside.mean() < 2


def test_full_render_with_blur(ffmpeg_paths, sample_clips, tmp_path):
    from core.editor.composition import default_composition
    from core.media.probe import probe_media
    from core.render.job import render
    from core.render.settings import RenderSettings

    info = probe_media(sample_clips["mp4"], ffmpeg_paths.ffprobe)
    timeline = Timeline.for_source(round(info.duration_s * S)).delete_range(800_000, 1_200_000)
    timeline = timeline.add_blur(0, 2 * S, Rect(0.3, 0.3, 0.4, 0.4))
    result = render(info, default_composition(1280, 720), timeline, RenderSettings(width=270, height=480),
                    tmp_path / "blur.mp4", ffmpeg_paths.ffmpeg, ffmpeg_paths.ffprobe)
    assert result.duration_s == pytest.approx(1.6, abs=0.1)


# --------------------------------------------------------------------- UI
def test_blur_on_timeline_source_view_and_panel(qapp):
    from ui.blur.blur_panel import BlurPanel
    from ui.editor.composition_controller import CompositionController
    from ui.preview.source_view import SourceView
    from ui.timeline.timeline_controller import BLUR, TimelineController
    from ui.timeline.timeline_view import BLUR_Y, TimelineView

    tc = TimelineController()
    tc.set_timeline(Timeline.for_source(10 * S))
    tc.set_playhead(2 * S)
    panel = BlurPanel(tc)
    assert not panel.effect_card.isEnabled() and "No blur yet" in panel.hint.text()
    panel.add_button.click()
    blur = tc.selected_blur()
    assert tc.selected == (BLUR, blur.id) and (blur.start_us, blur.end_us) == (2 * S, 5 * S)
    assert panel.list.count() == 1 and panel.effect_card.isEnabled()
    panel.strength_slider.setValue(90)
    panel.mode_combo.setCurrentIndex(1)
    assert tc.selected_blur().strength == 90 and tc.selected_blur().mode == PIXELATE
    tc.set_playhead(3 * S)
    panel.end_button.click()
    assert tc.selected_blur().end_us == 3 * S

    # The box is dragged on the Original view (half scale: 1 widget px = 2 source px).
    comp = CompositionController()
    view = SourceView(comp)
    view.set_blur_controller(tc)
    view.resize(960, 540)
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(QColor("#204060"))
    view.set_image(image)
    view.show()
    QApplication.processEvents()
    tc.set_playhead(2_500_000)
    box = view.source_to_widget(view.blur_px(tc.selected_blur()))
    drag(view, box.center(), box.center() + QPointF(50, 25))
    moved = tc.selected_blur().rect
    assert moved.x == pytest.approx(0.4 + 100 / 1920, abs=0.003) and moved.y == pytest.approx(0.4 + 50 / 1080, abs=0.003)
    box = view.source_to_widget(view.blur_px(tc.selected_blur()))
    drag(view, box.bottomRight(), box.bottomRight() + QPointF(40, 20))
    assert tc.selected_blur().rect.w == pytest.approx(0.2 + 80 / 1920, abs=0.003)

    # The Blur track: drag the right edge of the block to make it longer.
    tl = TimelineView(tc)
    tl.resize(1000, 260)
    tl.zoom_fit()
    region = tc.selected_blur()
    x_end = tl.us_to_x(region.end_us)
    drag(tl, QPointF(x_end, BLUR_Y + 10), QPointF(tl.us_to_x(6 * S), BLUR_Y + 10),
         Qt.KeyboardModifier.AltModifier)
    assert tc.selected_blur().end_us == pytest.approx(6 * S, abs=60_000)
    tc.select(None)
    QTest_click = tl.us_to_x(2_600_000)
    drag(tl, QPointF(QTest_click, BLUR_Y + 10), QPointF(QTest_click + 0.5, BLUR_Y + 10))
    assert tc.selected == (BLUR, region.id)
    panel.delete_button.click()
    assert tc.timeline.blurs == () and panel.list.count() == 0
    view.close()


def test_main_window_blur_and_follow(qapp, data_dir, ffmpeg_paths, moving_clip):
    from app.config import AppConfig
    from tests.test_ui import _wait
    from ui.main_window.main_window import MainWindow

    window = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    window.import_files([str(moving_clip)])
    assert _wait(lambda: window.timeline.timeline is not None and window.preview.media is not None)
    window.timeline.set_playhead(1_500_000)
    assert window.add_blur() and window.act_blur.shortcut().toString() == "B"
    blur = window.timeline.selected_blur()
    window.timeline.update_blur(blur.id, start_us=0, end_us=3 * S, rect=Rect(200 / 640, 120 / 360, 80 / 640, 60 / 360))
    assert window.history.can_undo
    assert window.blur_runner.start(blur.id)
    assert _wait(lambda: window.blur_runner.task is None, 20)
    followed = window.timeline.timeline.blur(blur.id)
    assert followed.track and "follows" in window.blur_panel.follow_status.text()
    assert followed.rect_at(0).x * 640 == pytest.approx(50, abs=5)
    window.blur_panel.unfollow_button.click()
    assert not window.timeline.timeline.blur(blur.id).track
    window._dirty = False
    window.close()

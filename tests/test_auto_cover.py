"""Auto cover (0.9.29): the frame with the strongest emotion on the streamer's
face, the face big on the cover, a large title away from it."""

from __future__ import annotations

import threading

import numpy as np
import pytest

from core.editor.composition import default_composition
from core.editor.geometry import Rect
from core.thumbnails.auto import (
    AutoCoverCancelled,
    CoverPick,
    _Sample,
    apply_pick,
    find_cover_frames,
    near_peak,
    sample_times,
    score_samples,
    search_region,
    sharpness,
)
from core.thumbnails.model import BOTTOM, TOP, ThumbnailDesign
from core.vision.emotion import LABELS, Emotion, face_crop, grey64, softmax
from core.vision.faces import Face

S = 1_000_000


def _emotion(name: str, p: float) -> Emotion:
    probs = [0.0] * len(LABELS)
    probs[0] = 1 - p
    probs[LABELS.index(name)] += p
    return Emotion(tuple(probs))


class FakeDetector:
    def __init__(self, face: Face | None = Face(0.4, 0.3, 0.6, 0.7, 0.95)):
        self.face = face

    def detect(self, rgb, search_tiles=True):
        return [] if self.face is None or rgb.mean() < 20 else [self.face]


class FakeEmotions:
    """Reads the 'emotion' from the frame's brightness: brighter = stronger."""

    def read(self, rgb, face):
        return _emotion("surprise", min(1.0, float(rgb.mean()) / 255))


def _grab_by_time(levels: dict[int, int]):
    def grab(ffmpeg, video, us, region, sw, sh):
        rgb = np.zeros((90, 160, 3), np.uint8)
        rgb[:] = levels.get(us, 0)
        rgb[::2, ::2] = np.clip(rgb[::2, ::2].astype(int) + 30, 0, 255)  # some detail for sharpness
        return rgb
    return grab


# ------------------------------------------------------------------ pieces

def test_emotion_strength_and_text():
    calm, laugh = _emotion("happiness", 0.05), _emotion("happiness", 0.9)
    assert laugh.strength > 0.85 > calm.strength and laugh.label == "happiness" and calm.label == "neutral"
    assert laugh.text == "happy 90%"
    assert _emotion("surprise", 0.8).strength > _emotion("sadness", 0.8).strength
    assert softmax(np.array([0.0, 0.0])).tolist() == [0.5, 0.5]


def test_face_crop_and_grey():
    rgb = np.zeros((100, 200, 3), np.uint8)
    crop = face_crop(rgb, Face(0.4, 0.4, 0.5, 0.6, 1.0))
    assert crop.shape[0] == crop.shape[1] and 20 <= crop.shape[0] <= 25  # square around a 20×20 face
    assert grey64(np.full((30, 30, 3), 200, np.uint8)).shape == (64, 64)
    flat, busy = np.full((40, 40, 3), 90, np.uint8), np.indices((40, 40)).sum(0)[..., None].repeat(3, 2) % 2 * 255
    assert sharpness(busy.astype(np.uint8)) > sharpness(flat) == 0


def test_sample_times_follow_peaks():
    times = sample_times(20 * S, lambda out: out + 10 * S, peaks=[14 * S])
    assert times == sorted(times) and len(times) <= 36
    assert 14_300_000 in times and 14_900_000 in times  # the reaction just after the peak
    assert all(b - a >= 400_000 for a, b in zip(times, times[1:]))
    assert min(times) >= 10 * S and max(times) < 30 * S + S
    assert sample_times(0, lambda out: out) == []


def test_scores_prefer_strong_emotion_and_faces():
    face = Face(0, 0, 1, 1, 1)
    samples = [_Sample(0), _Sample(1, face, 0.1, sharp=50, size=0.2), _Sample(2, face, 0.9, sharp=40, size=0.18),
               _Sample(3, near_peak=1.0)]
    scores = score_samples(samples, True)
    assert scores[2] > scores[1] > scores[3] > scores[0]
    no_model = score_samples(samples, False)
    assert min(no_model[1], no_model[2]) > max(no_model[0], no_model[3])
    assert near_peak(5 * S, [4 * S]) > near_peak(6 * S, [4 * S]) > 0 == near_peak(8 * S, [4 * S])


def test_main_face_prefers_confident():
    from core.thumbnails.auto import main_face

    real, shirt = Face(0.4, 0.1, 0.5, 0.25, 1.0), Face(0.3, 0.4, 0.5, 0.8, 0.89)
    assert main_face([shirt, real]) is real and main_face([]) is None
    duo = Face(0.6, 0.1, 0.75, 0.3, 0.98)
    assert main_face([real, duo]) is duo  # two sure faces: the bigger one


def test_search_region_stays_inside():
    r = search_region(Rect(0.0, 0.0, 0.3, 0.3))
    assert r.x == 0 and r.y == 0 and r.w == pytest.approx(0.345) and r.h == pytest.approx(0.345)
    r = search_region(Rect(0.8, 0.8, 0.2, 0.2))
    assert r.x + r.w <= 1.0 and r.y + r.h <= 1.0


def test_find_cover_frames_ranks_by_emotion():
    times = [1 * S, 2 * S, 3 * S, 4 * S]
    levels = {1 * S: 40, 2 * S: 200, 3 * S: 90, 4 * S: 0}  # 4 s: black, no face
    messages = []
    result = find_cover_frames("ffmpeg", "v.mp4", times, Rect(0.0, 0.0, 0.5, 0.5), 1920, 1080,
                               FakeDetector, FakeEmotions, peaks=[], progress=lambda f, m: messages.append(m),
                               grab=_grab_by_time(levels))
    assert result.with_emotions and result.sampled == 4
    assert [p.source_us for p in result.picks] == [2 * S, 3 * S, 1 * S, 4 * S]
    best = result.best
    assert best.emotion.startswith("surprised") and best.strength > 0.6
    # The face box is mapped from the searched region (webcam + margin) back to the whole frame.
    region = search_region(Rect(0.0, 0.0, 0.5, 0.5))
    assert best.face == pytest.approx((0.4 * region.w, 0.3 * region.h, 0.6 * region.w, 0.7 * region.h))
    assert result.picks[-1].face is None and result.picks[-1].note == "no face"
    assert any("frame 4 of 4" in m for m in messages)


def test_find_cover_frames_without_emotion_model_and_cancel():
    def broken():
        raise RuntimeError("offline")

    levels = {1 * S: 120, 2 * S: 120}
    result = find_cover_frames("ffmpeg", "v.mp4", [1 * S, 2 * S], Rect(0, 0, 0.3, 0.3), 1920, 1080,
                               FakeDetector, broken, peaks=[2 * S], grab=_grab_by_time(levels))
    assert not result.with_emotions and result.best.source_us == 2 * S  # the loud moment wins the tie
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(AutoCoverCancelled):
        find_cover_frames("ffmpeg", "v.mp4", [S], Rect(0, 0, 0.3, 0.3), 1920, 1080, FakeDetector, None,
                          cancel=cancel, grab=_grab_by_time({}))


def test_apply_pick_sets_up_the_cover():
    pick = CoverPick(3 * S, 0.9, (0.1, 0.1, 0.2, 0.25), "happy 90%", 0.9)
    design = apply_pick(ThumbnailDesign(size=120, uppercase=False, offset=10, zoom=1.5), pick, face_y=0.2)
    assert design.source_us == 3 * S and design.face == (0.1, 0.1, 0.2, 0.25) and design.face_cutout
    assert design.position == TOP and design.size == 170 and design.uppercase and design.offset == 0
    assert apply_pick(ThumbnailDesign(), pick, face_y=0.7).position == BOTTOM
    plain = apply_pick(ThumbnailDesign(position=BOTTOM), CoverPick(S, 0.1), None)
    assert plain.face is None and not plain.face_cutout and plain.position == BOTTOM


def test_design_keeps_face():
    design = ThumbnailDesign(face=(0.1, 0.2, 0.3, 0.4), face_cutout=True)
    assert ThumbnailDesign.from_dict(design.to_dict()) == design
    assert ThumbnailDesign.from_dict({"face": [0.1, 0.2, 0.3, 0.4], "face_cutout": True}) == design
    assert ThumbnailDesign.from_dict({"face": "x", "face_cutout": True}).face_cutout is False
    assert ThumbnailDesign(face=(0.5, 0.5, 0.5, 0.9)).clean().face is None


# ------------------------------------------------------------------ drawing

def test_face_in_output_and_cutout(qapp):
    from PySide6.QtGui import QColor, QImage

    from core.thumbnails.draw import compose_frame, draw_thumbnail, face_in_output

    comp = default_composition(1920, 1080)  # webcam: the top-left 30 % on top of the Short
    face = (0.1, 0.1, 0.2, 0.25)
    box = face_in_output(face, comp, 1920, 1080)
    assert box is not None and 0 < box.center().y() < comp.divider and 0 < box.center().x() < 1
    assert face_in_output(None, comp, 1920, 1080) is None

    source = QImage(1920, 1080, QImage.Format.Format_RGB32)
    source.fill(QColor("#2060a0"))
    frame = compose_frame(source, comp, 1920, 1080)
    design = ThumbnailDesign(text="Тест", face=face, face_cutout=False)
    plain = draw_thumbnail(frame, design, "")
    cut = draw_thumbnail(frame, ThumbnailDesign(text="Тест", face=face, face_cutout=True), "", box)
    assert plain.size() == cut.size() and plain != cut

    def white_pixels(image):
        small = image.scaled(108, 192)
        return sum(1 for y in range(small.height()) for x in range(small.width())
                   if QColor(small.pixel(x, y)).lightness() > 240)

    assert white_pixels(cut) > white_pixels(plain) + 20  # the white frame of the cut-out
    from core.thumbnails.model import LANDSCAPE

    wide = draw_thumbnail(frame, ThumbnailDesign(face=face, face_cutout=True, format=LANDSCAPE), "x", box)
    assert wide.width() == 1280


def test_variants_carry_faces(qapp):
    from core.thumbnails.variants import cover_variants

    faces = {S: (0.1, 0.1, 0.2, 0.2)}
    variants = cover_variants(ThumbnailDesign(), [S, 2 * S], faces=faces)
    assert variants[0].design.face == faces[S] and variants[0].design.face_cutout
    assert variants[2].design.face is None and not variants[2].design.face_cutout
    off = cover_variants(ThumbnailDesign(face=(0.3, 0.3, 0.4, 0.4), face_cutout=False), [S], faces=faces)
    assert off[0].design.face == faces[S] and not off[0].design.face_cutout  # the user turned it off


# ------------------------------------------------------------------ the Data page

def test_auto_cover_button(qapp, data_dir, ffmpeg_paths, sample_clips):
    from tests.test_steps import _wait, _window
    from ui.main_window.pages import DATA

    calls = []

    class CountingEmotions:
        def read(self, rgb, face):
            calls.append(1)
            return _emotion("happiness", 0.9 if len(calls) == 3 else 0.2)

    w = _window(data_dir, ffmpeg_paths)
    try:
        w.import_files([str(sample_clips["mp4"])])
        assert _wait(lambda: w.timeline.timeline is not None)
        w.set_page(DATA)
        keeper, editor = w.thumbnails, w.thumbnail_editor
        keeper.detector_factory = lambda: FakeDetector(Face(0.3, 0.2, 0.6, 0.7, 0.9))
        keeper.emotion_factory = CountingEmotions
        editor.auto_button.click()
        assert _wait(lambda: keeper.auto_task is None, 30)
        design = w.project.thumbnail
        picks = keeper.auto_picks[str(w.preview.media.path)]
        assert design.source_us == picks[0].source_us and picks[0].emotion == "happy 90%"
        assert design.face == picks[0].face and design.face_cutout and editor.face_check.isChecked()
        assert design.size >= 170 and "happy 90%" in editor.auto_status.text()
        assert _wait(lambda: len(editor.frame_buttons) >= 2 and editor.frame_buttons[0].property("source_us")
                     == picks[0].source_us)
        assert _wait(lambda: keeper.image is not None)
        editor.face_check.setChecked(False)
        assert not w.project.thumbnail.face_cutout
        editor.frame_buttons[1].click()  # another found frame keeps its own face box
        assert w.project.thumbnail.face == picks[1].face and not w.project.thumbnail.face_cutout
        editor.scrub.setValue(1)
        assert _wait(lambda: w.project.thumbnail.source_us not in {p.source_us for p in picks})
        assert w.project.thumbnail.face is None and not editor.face_check.isEnabled()
    finally:
        w._set_dirty(False)
        w.close()

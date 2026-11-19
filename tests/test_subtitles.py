"""Subtitles: captions from the transcript, masking, files, drawing, burn-in."""

from __future__ import annotations

import subprocess

import numpy as np
import pytest

from core.media.probe import probe_media
from core.subtitles.export import to_srt, to_vtt, write_subtitles
from core.subtitles.model import (
    MASK_ALL,
    MASK_NONE,
    PRESETS,
    SubtitleEdits,
    SubtitleSettings,
    SubtitleStyle,
    build_captions,
    caption_at,
    mask_word,
    wrap_lines,
)
from core.timeline.model import Segment, Timeline
from core.transcription.model import Segment as TSegment
from core.transcription.model import Transcript, Word

S = 1_000_000


def words(*items):
    return tuple(Word(int(a * S), int(b * S), t) for a, b, t in items)


def transcript() -> Transcript:
    seg1 = TSegment(1, int(0.5 * S), int(3.0 * S), "привет ребята это блин тест",
                    words((0.5, 0.8, "привет"), (0.9, 1.3, "ребята"), (1.4, 1.6, "это"),
                          (1.7, 2.0, "блин"), (2.1, 3.0, "тест.")))
    seg2 = TSegment(2, int(4.0 * S), int(5.0 * S), "дальше", words((4.0, 5.0, "дальше")))
    return Transcript("ru", 0.9, "small", (seg1, seg2), 6 * S)


def test_captions_group_map_to_output_and_mask():
    timeline = Timeline.for_source(6 * S)
    style = SubtitleStyle(enabled=True, max_words=3, uppercase=False)
    caps = build_captions(transcript(), timeline, style)
    assert [c.text for c in caps] == ["привет ребята это", "блин тест.", "дальше"]
    assert caps[0].start_us == int(0.5 * S)
    assert caps[0].end_us <= caps[1].start_us  # never overlap
    assert caps[0].word_at(int(1.0 * S)) == 1

    # Cut 1.0–1.5 s away (removes "это"), mute "блин".
    edited = Timeline.for_source(6 * S).delete_range(int(1.35 * S), int(1.65 * S))
    edited = edited.add_mute(int(1.7 * S), int(2.0 * S))
    caps = build_captions(transcript(), edited, style)
    texts = " ".join(c.text for c in caps)
    assert "это" not in texts and "б***" in texts
    first = caps[0].words[0]
    assert first.start_us == int(0.5 * S)
    blin = next(w for c in caps for w in c.words if w.masked)
    assert blin.start_us == edited.source_to_output(int(1.7 * S))

    upper = build_captions(transcript(), timeline, SubtitleStyle(enabled=True, mask=MASK_ALL, uppercase=True))
    assert upper[0].words[0].text == "ПРИВЕТ"


def test_edits_split_and_join():
    timeline = Timeline.for_source(6 * S)
    style = SubtitleStyle(enabled=True, max_words=3, uppercase=False)
    edits = SubtitleEdits().split_at(int(0.9 * S))  # start a caption at "ребята"
    caps = build_captions(transcript(), timeline, style, edits)
    assert caps[0].text == "привет" and caps[1].text.startswith("ребята")
    edits = SubtitleEdits().join_at(int(1.7 * S))  # glue "блин" to the first caption
    caps = build_captions(transcript(), timeline, style, edits)
    assert caps[0].text == "привет ребята это блин"
    again = SubtitleSettings.from_dict(SubtitleSettings(style, edits).to_dict())
    assert again.edits == edits and again.style == style


def test_style_round_trip_and_cleaning():
    style = SubtitleStyle.from_dict({"size": 9999, "max_words": 0, "mask": "weird", "bold": "yes", "font": "Arial"})
    assert style.size == 200 and style.max_words == 1 and style.mask == "first" and style.bold is True
    assert style.font == "Arial"
    assert SubtitleStyle.from_dict(PRESETS["Boxed"].to_dict()) == PRESETS["Boxed"]
    assert SubtitleStyle.from_dict(None) == SubtitleStyle()


def test_mask_and_wrap():
    assert mask_word("блин!", "first") == "б***!"
    assert mask_word("блин", MASK_ALL) == "****"
    assert mask_word("блин", MASK_NONE) == "блин"
    assert wrap_lines(["aaaa", "bbbb", "cccc"], 9, 2) == [[0, 1], [2]]
    assert wrap_lines(["a"] * 5, 1, 2) == [[0], [1, 2, 3, 4]]


def test_srt_and_vtt(tmp_path):
    caps = build_captions(transcript(), Timeline.for_source(6 * S), SubtitleStyle(uppercase=False))
    srt = to_srt(caps)
    assert srt.startswith("1\n00:00:00,500 --> ") and "привет" in srt
    assert to_vtt(caps).startswith("WEBVTT")
    path = write_subtitles(tmp_path / "x.srt", caps)
    assert path.read_text(encoding="utf-8-sig") == srt
    assert caption_at(caps, int(4.5 * S)).text == "дальше" and caption_at(caps, int(3.9 * S)) is None


def test_draw_caption_paints_text(qapp):
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter

    from core.subtitles.raster import draw_caption

    caps = build_captions(transcript(), Timeline.for_source(6 * S), SubtitleStyle(enabled=True))
    image = QImage(270, 480, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    draw_caption(painter, QRectF(0, 0, 270, 480), caps[0], SubtitleStyle(enabled=True), int(0.95 * S), 0.35)
    painter.end()
    alpha = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(480, 270, 4)[:, :, 3]
    rows = np.nonzero(alpha.max(axis=1))[0]
    assert rows.size and 100 < rows.mean() < 250  # around the divider (35 % of 480 = 168)


def test_burn_in_render(sample_clips, ffmpeg_paths, tmp_path):
    from core.editor.composition import default_composition
    from core.render.job import render
    from core.render.settings import RenderSettings

    info = probe_media(sample_clips["mp4"], ffmpeg_paths.ffprobe)
    timeline = Timeline.for_source(round(info.duration_s * S))
    t = Transcript("ru", 0.9, "small", (TSegment(1, int(0.2 * S), int(1.5 * S), "Привет мир",
                                                  words((0.2, 0.7, "Привет"), (0.8, 1.5, "мир"))),), 2 * S)
    style = SubtitleStyle(enabled=True, size=120, color="#ff0000", highlight="#ff0000", outline_px=0,
                          shadow=False, position=0.5)
    caps = build_captions(t, timeline, style)
    out = render(info, default_composition(1280, 720), timeline, RenderSettings(width=270, height=480, fps=30),
                 tmp_path / "subs.mp4", ffmpeg_paths.ffmpeg, ffmpeg_paths.ffprobe, None,
                 captions=caps, subtitle_style=style)
    assert out.path.is_file()

    def frame(at: float) -> np.ndarray:
        raw = subprocess.run(
            [str(ffmpeg_paths.ffmpeg), "-v", "error", "-ss", f"{at}", "-i", str(out.path), "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        return np.frombuffer(raw, dtype=np.uint8).reshape(480, 270, 3).astype(int)

    def red_pixels(img: np.ndarray) -> int:
        band = img[200:280]
        return int(((band[:, :, 0] > 200) & (band[:, :, 1] < 60) & (band[:, :, 2] < 60)).sum())

    assert red_pixels(frame(0.5)) > 50  # caption burnt in at the centre
    assert red_pixels(frame(1.9)) < red_pixels(frame(0.5)) // 4  # gone after the caption


# ------------------------------------------------------------------ UI
def _wait(predicate, timeout=10.0):
    import time

    from PySide6.QtWidgets import QApplication

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _clip_transcript() -> Transcript:
    seg = TSegment(0, int(0.1 * S), int(1.6 * S), "раз два три четыре",
                   words((0.1, 0.3, "раз"), (0.4, 0.6, "два"), (0.7, 1.0, "три"), (1.1, 1.6, "четыре")))
    return Transcript("ru", 0.9, "small", (seg,), 2 * S)


def test_panel_round_trip_and_presets(qapp):
    from ui.subtitles.subtitles_panel import SubtitlesPanel

    panel = SubtitlesPanel()
    seen = []
    panel.settings_changed.connect(seen.append)
    settings = SubtitleSettings(SubtitleStyle(enabled=True, size=90, max_words=2, color="#00ff00"),
                                SubtitleEdits().split_at(5))
    panel.set_settings(settings)
    assert panel.settings() == settings and not seen  # loading is silent
    panel.words_spin.setValue(4)
    assert seen and seen[-1].style.max_words == 4
    panel.preset_combo.activated.emit(list(PRESETS).index("Boxed"))
    assert panel.style().box and panel.settings().edits == settings.edits  # a preset keeps the edits
    panel.set_has_transcript(False)
    panel.set_has_transcript(True)
    panel.deleteLater()


@pytest.fixture
def window(qapp, data_dir, ffmpeg_paths, sample_clips):
    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    w.resize(1400, 900)
    w.show()
    w.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: w.timeline.timeline is not None)
    yield w
    w._dirty = False
    w.close()


def test_window_subtitles_flow(window, tmp_path, data_dir, ffmpeg_paths):
    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow

    panel = window.subtitles_panel
    assert window.captions == []
    window.transcript.set_transcript(_clip_transcript())
    panel.enabled_check.setChecked(True)
    panel.words_spin.setValue(3)
    assert [c.text for c in window.captions] == ["РАЗ ДВА ТРИ", "ЧЕТЫРЕ"]
    assert panel.list.count() == 2
    assert window.project.subtitles.style.enabled

    # The preview shows the caption under the playhead.
    window.timeline.set_playhead(int(0.5 * S))
    window._update_subtitle_time(int(0.5 * S))
    assert window.preview.output_view.current_caption().text == "РАЗ ДВА ТРИ"

    # Split at the playhead starts a caption at "два".
    assert window.split_caption_at_playhead()
    assert [c.text for c in window.captions] == ["РАЗ", "ДВА ТРИ ЧЕТЫРЕ"]

    path = window.save_subtitles(tmp_path / "clip.srt")
    assert path.read_text(encoding="utf-8-sig").startswith("1\n00:00:00,100 --> ")

    window.project_path = tmp_path / "s.tssproj"
    assert window.save()
    other = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    other.open_project(tmp_path / "s.tssproj")
    assert _wait(lambda: other.subtitles_panel.settings() == window.project.subtitles)
    other._dirty = False
    other.close()


def test_export_dialog_subtitle_checkbox(qapp, sample_clips, ffmpeg_paths, tmp_path):
    from core.editor.composition import default_composition
    from core.render.settings import RenderSettings
    from ui.export.export_dialog import ExportDialog

    info = probe_media(sample_clips["mp4"], ffmpeg_paths.ffprobe)
    timeline = Timeline.for_source(round(info.duration_s * S))
    style = SubtitleStyle(enabled=True)
    caps = build_captions(_clip_transcript(), timeline, style)
    args = (info, default_composition(1280, 720), timeline, ffmpeg_paths, RenderSettings(), tmp_path / "o.mp4")
    dialog = ExportDialog(*args, captions=caps, subtitle_style=style)
    assert dialog.subtitles_check.isEnabled() and dialog.subtitles_check.isChecked()
    assert dialog._subtitle_args()["captions"] == caps
    dialog.subtitles_check.setChecked(False)
    assert not dialog._subtitle_args()
    dialog.deleteLater()
    empty = ExportDialog(*args)
    assert not empty.subtitles_check.isEnabled() and not empty._subtitle_args()
    empty.deleteLater()

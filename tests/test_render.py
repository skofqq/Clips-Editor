"""Phase 6: filter graph, encoders, rendering, validation and the export dialog."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from core.editor.composition import WEBCAM, default_composition
from core.media.probe import probe_media
from core.render import job as job_mod
from core.render.encoders import (
    EncoderInfo,
    choose_encoder,
    encoder_info,
    video_args,
)
from core.render.graph import build_graph, output_fps
from core.render.job import RenderCancelled, RenderError, partial_path, render, validate_output
from core.render.settings import (
    AUTO,
    BITRATE,
    CPU,
    CUSTOM_PRESET,
    DEFAULT_PRESET,
    HEVC,
    MKV,
    OPUS,
    PRESETS,
    RenderSettings,
    preset_name,
)
from core.timeline.model import BEEP, Timeline

S = 1_000_000


@pytest.fixture(scope="module")
def mp4(sample_clips, ffmpeg_paths):
    return probe_media(sample_clips["mp4"], ffmpeg_paths.ffprobe)


def _edit(duration_us: int) -> Timeline:
    # Keep 0-0.8 s and 1.2-2.0 s; silence 0.2-0.5 s; beep 1.4-1.7 s (source time).
    t = Timeline.for_source(duration_us).delete_range(800_000, 1_200_000)
    return t.add_mute(200_000, 500_000).add_mute(1_400_000, 1_700_000, BEEP)


def _audio(path: Path, ffmpeg) -> np.ndarray:
    import subprocess

    raw = subprocess.run(
        [str(ffmpeg), "-loglevel", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "48000", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def _level(audio: np.ndarray, start: float, end: float) -> tuple[float, float]:
    x = audio[int(start * 48000):int(end * 48000)]
    spectrum = np.abs(np.fft.rfft(x))
    freq = np.fft.rfftfreq(len(x), 1 / 48000)[spectrum.argmax()]
    return float(np.sqrt((x ** 2).mean())), float(freq)


# ------------------------------------------------------------------ settings
def test_settings_clean_roundtrip_and_presets():
    s = RenderSettings(width=1081, height=1919, fps=29, video_codec="vp9", quality=99, encoder="").cleaned()
    assert (s.width, s.height, s.fps, s.video_codec, s.quality, s.encoder) == (1080, 1918, 0, "h264", 51, AUTO)
    assert RenderSettings.from_dict(s.to_dict()) == s
    assert RenderSettings.from_dict({"width": "bad"}) == RenderSettings()
    assert RenderSettings.from_dict(None) == RenderSettings()
    assert preset_name(RenderSettings()) == DEFAULT_PRESET
    assert preset_name(RenderSettings(fps=60, width=720, height=1280)) == DEFAULT_PRESET
    assert preset_name(RenderSettings(quality=30)) == CUSTOM_PRESET
    assert PRESETS["High Quality · H.265 MKV"].container == MKV


def test_encoder_choice_and_arguments():
    nvenc = [EncoderInfo("h264_nvenc", "h264", "nvenc"), EncoderInfo("libx264", "h264", "cpu")]
    assert choose_encoder(RenderSettings(), nvenc) == "h264_nvenc"
    assert choose_encoder(RenderSettings(encoder=CPU), nvenc) == "libx264"
    assert choose_encoder(RenderSettings(video_codec=HEVC), nvenc) == "libx265"  # no HEVC GPU listed
    assert choose_encoder(RenderSettings(encoder="h264_amf"), nvenc) == "h264_nvenc"  # not usable here
    assert choose_encoder(RenderSettings(), None) == "libx264"
    assert encoder_info("hevc_qsv").kind == "qsv" and encoder_info("libx265").codec == HEVC

    args = video_args("h264_nvenc", RenderSettings(quality=21), 30)
    assert args[args.index("-cq") + 1] == "21" and "-crf" not in args
    args = video_args("libx264", RenderSettings(rate_control=BITRATE, video_bitrate_k=8000), 60)
    assert args[args.index("-b:v") + 1] == "8000k" and args[args.index("-g") + 1] == "120"
    assert "-global_quality" in video_args("h264_qsv", RenderSettings(), 30)
    assert "cqp" in video_args("hevc_amf", RenderSettings(video_codec=HEVC), 30)


def test_ffmpeg_version_selects_filter_script_option(monkeypatch, tmp_path):
    from core.render.graph import RenderGraph
    from fractions import Fraction

    graph = RenderGraph("nullsrc", "v", None, 1.0, Fraction(30))
    for line, expected in (
        ("ffmpeg version 6.1.1-3ubuntu5 Copyright", "-filter_complex_script"),
        ("ffmpeg version n7.1-8-g1234 Copyright", "-/filter_complex"),
        ("ffmpeg version N-117000-gabcdef-20250101 Copyright", "-/filter_complex"),
    ):
        monkeypatch.setattr(job_mod, "tool_version", lambda path, l=line: l)
        args = job_mod._filter_args(graph, Path("ffmpeg"), tmp_path)
        assert args[0] == expected
        assert (tmp_path / "filter.txt").read_text(encoding="utf-8") == "nullsrc"


# ------------------------------------------------------------------ graph
def test_graph_follows_edit_and_layout(mp4):
    comp = default_composition(1280, 720)
    timeline = _edit(round(mp4.duration_s * S)).with_gain_db(-3).with_fades(100_000, 200_000)
    graph = build_graph(comp, timeline, mp4, RenderSettings())
    text = graph.filter_complex
    assert graph.duration_s == pytest.approx(1.6)
    assert graph.fps == 30
    assert "trim=start=0.000000:end=0.800000" in text and "trim=start=1.200000:end=2.000000" in text
    assert "concat=n=2:v=1:a=0" in text and "concat=n=2:v=0:a=1" in text
    for p in comp.layout(1280, 720):  # every region is cropped to its source rectangle
        assert f"crop={round(p.src.w)}:{round(p.src.h)}:{round(p.src.x)}:{round(p.src.y)}" in text
    assert "between(t,0.200000,0.500000)" in text and "sin(2*PI*1000*t)" in text
    assert "volume=-3.00dB" in text and "afade=t=in:st=0:d=0.100000" in text
    assert "afade=t=out:st=1.400000:d=0.200000" in text
    assert graph.audio_label == "aout" and graph.video_label == "vout"

    # Silence only: no tone. Other output sizes are padded, not stretched.
    silent = Timeline.for_source(2 * S).add_mute(0, 300_000)
    g2 = build_graph(comp, silent, mp4, RenderSettings(width=1080, height=1080, fps=60))
    assert "sin(" not in g2.filter_complex and "pad=1080:1080" in g2.filter_complex and g2.fps == 60

    # One region disabled: a single overlay on the full canvas.
    one = comp.with_region(WEBCAM, comp.webcam.__class__(comp.webcam.crop, enabled=False))
    assert build_graph(one, silent, mp4, RenderSettings()).filter_complex.count("overlay=") == 1


def test_output_fps_caps_source(sample_clips, ffmpeg_paths):
    info = probe_media(sample_clips["mkv_noaudio"], ffmpeg_paths.ffprobe)
    assert output_fps(RenderSettings(), info) == 60
    assert output_fps(RenderSettings(fps=25), info) == 25
    graph = build_graph(default_composition(640, 360), Timeline.for_source(S), info, RenderSettings())
    assert graph.audio_label is None


# ------------------------------------------------------------------ render
def test_render_h264_mp4_matches_edit(mp4, ffmpeg_paths, tmp_path):
    timeline = _edit(round(mp4.duration_s * S))
    progress = []
    result = render(
        mp4, default_composition(1280, 720), timeline, RenderSettings(width=540, height=960, fps=30),
        tmp_path / "out.mp4", ffmpeg_paths.ffmpeg, ffmpeg_paths.ffprobe, None,
        progress=lambda f, m: progress.append((f, m)),
    )
    assert result.path == tmp_path / "out.mp4" and result.path.is_file()
    assert not partial_path(result.path).exists()
    assert result.encoder == "libx264" and result.notice == ""
    assert (result.info.video.width, result.info.video.height) == (540, 960)
    assert result.info.video.codec == "h264" and result.info.audio[0].codec == "aac"
    assert result.duration_s == pytest.approx(1.6, abs=0.1)
    assert progress[-1] == (1.0, "Done") and any("fps" in m or "%" in m for _, m in progress)

    audio = _audio(result.path, ffmpeg_paths.ffmpeg)
    rms, freq = _level(audio, 0.05, 0.18)
    assert rms > 0.02 and freq == pytest.approx(440, abs=10)  # source tone
    assert _level(audio, 0.25, 0.45)[0] < 0.002  # silence mute
    # Source 1.4-1.7 s is output 1.0-1.3 s (0.4 s cut before it): the beep.
    rms, freq = _level(audio, 1.05, 1.25)
    assert rms > 0.05 and freq == pytest.approx(1000, abs=10)
    rms, freq = _level(audio, 1.35, 1.55)
    assert freq == pytest.approx(440, abs=10)


def test_render_hevc_mkv_opus(mp4, ffmpeg_paths, tmp_path):
    settings = RenderSettings(width=360, height=640, video_codec=HEVC, container=MKV, audio_codec=OPUS)
    result = render(
        mp4, default_composition(1280, 720), Timeline.for_source(round(mp4.duration_s * S)), settings,
        tmp_path / "hq.mp4", ffmpeg_paths.ffmpeg, ffmpeg_paths.ffprobe, None,
    )
    assert result.path.suffix == ".mkv"  # extension follows the container
    assert result.info.video.codec == "hevc" and result.info.audio[0].codec == "opus"


def test_render_without_audio(sample_clips, ffmpeg_paths, tmp_path):
    info = probe_media(sample_clips["mkv_noaudio"], ffmpeg_paths.ffprobe)
    result = render(
        info, default_composition(640, 360), Timeline.for_source(S), RenderSettings(width=270, height=480),
        tmp_path / "silent.mp4", ffmpeg_paths.ffmpeg, ffmpeg_paths.ffprobe,
    )
    assert result.info.audio == () and result.info.video.avg_fps == 60


def test_gpu_failure_falls_back_to_cpu(mp4, ffmpeg_paths, tmp_path, monkeypatch):
    # Pretend a GPU encoder is usable but make it fail when used.
    original = job_mod.video_args

    def broken(encoder, settings, fps):
        args = original(encoder, settings, fps)
        return args + ["-no_such_option", "1"] if encoder == "h264_nvenc" else args

    monkeypatch.setattr(job_mod, "video_args", broken)
    result = render(
        mp4, default_composition(1280, 720), Timeline.for_source(S), RenderSettings(width=270, height=480),
        tmp_path / "gpu.mp4", ffmpeg_paths.ffmpeg, ffmpeg_paths.ffprobe,
        [EncoderInfo("h264_nvenc", "h264", "nvenc")],
    )
    assert result.encoder == "libx264" and "rendered on the CPU" in result.notice


def test_render_errors_and_cancel(mp4, ffmpeg_paths, tmp_path):
    comp = default_composition(1280, 720)
    timeline = Timeline.for_source(round(mp4.duration_s * S))
    with pytest.raises(RenderError):
        render(mp4, comp, timeline, RenderSettings(), Path(mp4.path), ffmpeg_paths.ffmpeg, ffmpeg_paths.ffprobe)

    cancel = threading.Event()
    cancel.set()
    out = tmp_path / "cancelled.mp4"
    with pytest.raises(RenderCancelled):
        render(mp4, comp, timeline, RenderSettings(), out, ffmpeg_paths.ffmpeg, ffmpeg_paths.ffprobe,
               cancel=cancel)
    assert not out.exists() and not partial_path(out).exists()


def test_validation_rejects_wrong_output(mp4, ffmpeg_paths, tmp_path):
    with pytest.raises(RenderError, match="did not write"):
        validate_output(tmp_path / "none.mp4", ffmpeg_paths.ffprobe, RenderSettings(), 2.0, True)
    source = Path(mp4.path)
    with pytest.raises(RenderError, match="Expected 1080×1920"):
        validate_output(source, ffmpeg_paths.ffprobe, RenderSettings(), 2.0, True)
    with pytest.raises(RenderError, match="long"):
        validate_output(source, ffmpeg_paths.ffprobe, RenderSettings(width=1280, height=720), 9.0, True)
    assert validate_output(source, ffmpeg_paths.ffprobe, RenderSettings(width=1280, height=720), 2.0, True)


def test_detect_encoders_always_has_software(ffmpeg_paths):
    from core.render.encoders import detect_encoders

    names = [e.name for e in detect_encoders(ffmpeg_paths.ffmpeg)]
    assert "libx264" in names


# ------------------------------------------------------------------ UI
def _wait(predicate, timeout=30.0):
    from PySide6.QtWidgets import QApplication

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_export_dialog_settings_and_render(qapp, mp4, ffmpeg_paths, tmp_path):
    from ui.export.export_dialog import ExportDialog, default_output_path

    out = default_output_path(Path(mp4.path), str(tmp_path), ".mp4")
    assert out == tmp_path / "stream clip_short.mp4"
    out.write_bytes(b"x")
    assert default_output_path(Path(mp4.path), str(tmp_path), ".mp4").name == "stream clip_short_2.mp4"
    out.unlink()

    encoders = [EncoderInfo("h264_nvenc", "h264", "nvenc"), EncoderInfo("libx264", "h264", "cpu"),
                EncoderInfo("libx265", "hevc", "cpu")]
    dialog = ExportDialog(
        mp4, default_composition(1280, 720), _edit(round(mp4.duration_s * S)), ffmpeg_paths,
        RenderSettings(), out, encoders,
    )
    assert dialog.preset_combo.currentData() == DEFAULT_PRESET
    assert dialog.resolution_combo.currentData() == "1080x1920" and not dialog.width_spin.isEnabled()
    dialog.resolution_combo.setCurrentIndex(dialog.resolution_combo.findData("720x1280"))
    assert dialog.settings().width == 720
    assert "NVIDIA NVENC" in dialog.encoder_combo.currentText()
    assert "2 kept parts" in dialog.summary_label.text() and "2 mute regions" in dialog.summary_label.text()

    dialog.preset_combo.setCurrentIndex(dialog.preset_combo.findData("High Quality · H.265 MKV"))
    s = dialog.settings()
    assert (s.video_codec, s.container, s.audio_codec) == (HEVC, MKV, OPUS)
    assert dialog.output_edit.text().endswith(".mkv")
    assert dialog.encoder_combo.currentData() == AUTO and "CPU" in dialog.encoder_combo.currentText()
    dialog.quality_spin.setValue(30)
    assert dialog.preset_combo.currentData() == CUSTOM_PRESET

    dialog.set_settings(RenderSettings(width=270, height=480, encoder=CPU))
    assert dialog.resolution_combo.currentData() == "" and dialog.width_spin.isEnabled()
    assert "padded" in dialog.summary_label.text()
    dialog.start_render()
    assert dialog.cancel_button.isVisibleTo(dialog) and not dialog.close_button.isEnabled()
    assert _wait(lambda: dialog._task is None)
    assert dialog.result_path == out and out.is_file()
    assert "Done" in dialog.status_label.text() and dialog.open_button.isVisibleTo(dialog)
    dialog.close()


def test_main_window_export_action(qapp, data_dir, ffmpeg_paths, sample_clips, monkeypatch):
    from app.config import AppConfig
    from ui.export.export_dialog import ExportDialog
    from ui.main_window.main_window import MainWindow

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    assert not w.act_export.isEnabled()
    w.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: w.timeline.timeline is not None)
    assert w.act_export.isEnabled()
    assert _wait(lambda: w.encoders is not None)
    assert any(e.name == "libx264" for e in w.encoders)

    seen = {}

    def fake_exec(dialog):
        seen["settings"] = dialog.settings()
        seen["output"] = dialog.output_path()
        dialog.set_settings(RenderSettings(width=720, height=1280, quality=22))
        return 0

    monkeypatch.setattr(ExportDialog, "exec", fake_exec)
    w.act_export.trigger()
    assert seen["settings"].width == 1080 and seen["output"].name == "stream clip_short.mp4"
    assert w.config.export_settings["width"] == 720
    assert w.config.export_dir == str(Path(sample_clips["mp4"]).parent)
    w._dirty = False
    w.close()

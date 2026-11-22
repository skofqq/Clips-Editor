"""Loudness normalisation (-14 LUFS) and voice noise reduction."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

from core.audio.enhance import (
    DENOISE_STRONG,
    LOUDNESS_OFF,
    Loudness,
    MeasureCancelled,
    apply_filter,
    denoise_filter,
    loudness_report,
    measure_file,
    measure_filter,
    measure_loudness,
    parse_loudnorm,
    render_preview,
)
from core.editor.composition import default_composition
from core.media.probe import probe_media
from core.render.graph import build_graph, loudness_graph
from core.render.job import render
from core.render.settings import RenderSettings, preset_name
from core.timeline.model import Timeline

S = 1_000_000

LOUDNORM_OUTPUT = """
[Parsed_loudnorm_0 @ 0x55]
{
	"input_i" : "-31.42",
	"input_tp" : "-24.10",
	"input_lra" : "1.20",
	"input_thresh" : "-41.50",
	"output_i" : "-14.20",
	"output_tp" : "-6.00",
	"output_lra" : "1.00",
	"output_thresh" : "-24.40",
	"normalization_type" : "linear",
	"target_offset" : "0.20"
}
"""


@pytest.fixture(scope="module")
def quiet_voice(tmp_path_factory, ffmpeg_paths) -> Path:
    """4 s: a quiet 300 Hz 'voice' (about -32 dBFS) over steady hiss."""
    path = tmp_path_factory.mktemp("enh") / "quiet.mp4"
    subprocess.run([
        str(ffmpeg_paths.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=4,volume=0.035",
        "-f", "lavfi", "-i", "anoisesrc=color=white:amplitude=0.004:sample_rate=48000:duration=4",
        "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[a]", "-map", "0:v", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", str(path),
    ], check=True)
    return path


@pytest.fixture(scope="module")
def noise_only(tmp_path_factory, ffmpeg_paths) -> Path:
    path = tmp_path_factory.mktemp("enh") / "noise.wav"
    subprocess.run([
        str(ffmpeg_paths.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.05:sample_rate=48000:duration=4",
        str(path),
    ], check=True)
    return path


def _integrated(path: Path, ffmpeg) -> float:
    out = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(path), "-vn", "-af", measure_filter(-14), "-f", "null",
         "-"], capture_output=True, text=True,
    ).stderr
    return parse_loudnorm(out).input_i


def _rms(path: Path, ffmpeg, start: float = 1.0, end: float = 3.5) -> float:
    raw = subprocess.run([str(ffmpeg), "-loglevel", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar",
                          "48000", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.float32)[int(start * 48000):int(end * 48000)]
    return float(np.sqrt((x ** 2).mean()))


# ------------------------------------------------------------------ pieces

def test_parse_loudnorm_and_filters():
    m = parse_loudnorm("noise\n" + LOUDNORM_OUTPUT)
    assert m == Loudness(-31.42, -24.10, 1.20, -41.50, 0.20) and not m.silent
    assert parse_loudnorm("nothing here") is None
    assert parse_loudnorm(LOUDNORM_OUTPUT.replace('"-31.42"', '"-inf"')).silent
    second = apply_filter(-14, m)
    assert "measured_I=-31.42" in second and "linear=true" in second and second.endswith("aresample=48000")
    assert "measured" not in apply_filter(-14, None)
    assert denoise_filter(0) == "" and "afftdn" in denoise_filter(DENOISE_STRONG)


def test_timeline_denoise_round_trip():
    t = Timeline.for_source(3 * S)
    assert t.with_denoise(0) is t and "denoise" not in t.to_dict()
    t2 = t.with_denoise(2)
    assert t2.denoise == 2 and Timeline.from_dict(t2.to_dict()) == t2
    assert t.with_denoise(9).denoise == 3


def test_settings_loudness_independent_of_preset():
    s = RenderSettings()
    assert s.loudness_lufs == -14.0
    assert preset_name(s) == preset_name(RenderSettings(loudness_lufs=LOUDNESS_OFF))
    assert RenderSettings(loudness_lufs=-50).cleaned().loudness_lufs == -31.0
    assert RenderSettings.from_dict({"loudness_lufs": 0}).loudness_lufs == 0.0


def test_graph_places_cleanup_and_loudness(quiet_voice, ffmpeg_paths):
    info = probe_media(quiet_voice, ffmpeg_paths.ffprobe)
    t = Timeline.for_source(4 * S).with_denoise(2).with_fades(500_000, 500_000)
    graph = build_graph(default_composition(640, 360), t, info, RenderSettings(), loudnorm="loudnorm=I=-14")
    audio = graph.filter_complex.split("[0:a:0]", 1)[1]
    # Noise reduction first (source), loudness before the fades (output).
    assert audio.index("afftdn") < audio.index("loudnorm=I=-14") < audio.index("afade=t=in")
    assert "print_format=json" in loudness_graph(t, RenderSettings())


# ------------------------------------------------------------------ results

def test_render_normalises_to_minus_14(quiet_voice, ffmpeg_paths, tmp_path):
    info = probe_media(quiet_voice, ffmpeg_paths.ffprobe)
    before = _integrated(quiet_voice, ffmpeg_paths.ffmpeg)
    assert before < -25
    t = Timeline.for_source(round(info.duration_s * S))
    messages = []
    result = render(info, default_composition(640, 360), t, RenderSettings(width=360, height=640),
                    tmp_path / "loud.mp4", ffmpeg_paths.ffmpeg, ffmpeg_paths.ffprobe,
                    progress=lambda f, m: messages.append(m))
    assert any("loudness" in m.lower() for m in messages)
    assert _integrated(result.path, ffmpeg_paths.ffmpeg) == pytest.approx(-14, abs=1.0)
    # The finished file is measured again and the result is reported.
    assert result.loudness is not None and result.loudness.input_i == pytest.approx(-14, abs=1.0)
    assert result.loudness.input_tp <= -0.5
    assert "LUFS" in loudness_report(result.loudness, -14) and "✓" in loudness_report(result.loudness, -14)

    off = render(info, default_composition(640, 360), t, RenderSettings(width=360, height=640, loudness_lufs=0),
                 tmp_path / "off.mp4", ffmpeg_paths.ffmpeg, ffmpeg_paths.ffprobe)
    assert _integrated(off.path, ffmpeg_paths.ffmpeg) == pytest.approx(before, abs=1.0)
    assert off.loudness is None  # no check when normalisation is off


def test_measure_file_and_report(quiet_voice, noise_only, ffmpeg_paths, tmp_path):
    measured = measure_file(ffmpeg_paths.ffmpeg, quiet_voice)
    assert measured is not None and measured.input_i < -25
    report = loudness_report(measured, -14)
    assert "quieter than the target" in report and "✓" not in report
    assert "peaks above" in loudness_report(Loudness(-14, 0.3, 5, -24, 0), -14)
    assert "louder" in loudness_report(Loudness(-9, -1.2, 5, -20, 0), -14)
    assert loudness_report(measured, 0) == "" and loudness_report(None, -14) == ""
    assert "silent" in loudness_report(Loudness(float("-inf"), -90, 0, -70, 0), -14)
    silent_video = tmp_path / "novoice.mp4"
    subprocess.run([str(ffmpeg_paths.ffmpeg), "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=160x90:rate=10:duration=1", "-c:v", "libx264", str(silent_video)], check=True)
    assert measure_file(ffmpeg_paths.ffmpeg, silent_video) is None  # no audio track
    cancel = threading.Event()
    cancel.set()
    assert measure_file(ffmpeg_paths.ffmpeg, noise_only, cancel=cancel) is None


def test_loudness_measurement_cancel(quiet_voice, ffmpeg_paths, tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(MeasureCancelled):
        measure_loudness(ffmpeg_paths.ffmpeg, quiet_voice, loudness_graph(Timeline.for_source(4 * S),
                         RenderSettings()), "aout", tmp_path, cancel)
    measured = measure_loudness(ffmpeg_paths.ffmpeg, quiet_voice,
                                loudness_graph(Timeline.for_source(4 * S), RenderSettings()), "aout", tmp_path)
    assert measured is not None and measured.input_i < -25


def test_denoise_lowers_hiss(noise_only, ffmpeg_paths, tmp_path):
    plain = render_preview(ffmpeg_paths.ffmpeg, noise_only, 0, 4, tmp_path / "plain.wav")
    clean = render_preview(ffmpeg_paths.ffmpeg, noise_only, 0, 4, tmp_path / "clean.wav", denoise=DENOISE_STRONG)
    assert _rms(clean, ffmpeg_paths.ffmpeg) < _rms(plain, ffmpeg_paths.ffmpeg) * 0.5


def test_listen_preview_is_loud(quiet_voice, ffmpeg_paths, tmp_path):
    out = render_preview(ffmpeg_paths.ffmpeg, quiet_voice, 0.5, 3, tmp_path / "l.wav", denoise=1,
                         loudness_lufs=-14)
    assert _rms(out, ffmpeg_paths.ffmpeg, 0.5, 2.5) > _rms(quiet_voice, ffmpeg_paths.ffmpeg, 1.0, 3.0) * 2


# ------------------------------------------------------------------ UI

def test_timeline_panel_noise_and_export_loudness(qapp, data_dir, ffmpeg_paths, sample_clips, monkeypatch):
    from app.config import AppConfig
    from tests.test_ui import _wait
    from ui.main_window.main_window import MainWindow

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    w.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: w.timeline.timeline is not None)
    panel = w.timeline_panel
    assert panel.denoise_combo.isEnabled() and panel.denoise_combo.currentData() == 0
    panel.denoise_combo.setCurrentIndex(panel.denoise_combo.findData(2))
    panel.denoise_combo.activated.emit(panel.denoise_combo.currentIndex())
    assert w.timeline.timeline.denoise == 2 and w.project.timelines and w.act_undo.isEnabled()
    w.act_undo.trigger()
    assert w.timeline.timeline.denoise == 0 and panel.denoise_combo.currentData() == 0

    # Listen renders a cleaned-up snippet in the background.
    ready = []
    monkeypatch.setattr(type(w.listen), "_ready", lambda self, wk, r, path: ready.append(path))
    panel.listen_button.setChecked(True)
    assert _wait(lambda: bool(ready), 15)
    assert Path(ready[0]).is_file()
    w._set_dirty(False)
    w.close()


def test_export_dialog_loudness_combo(qapp, sample_clips, ffmpeg_paths):
    from ui.export.export_dialog import ExportDialog

    info = probe_media(sample_clips["mp4"], ffmpeg_paths.ffprobe)
    dialog = ExportDialog(info, default_composition(1280, 720), Timeline.for_source(2 * S), ffmpeg_paths,
                          RenderSettings(), Path("out.mp4"), [])
    assert dialog.settings().loudness_lufs == -14.0
    dialog.set_settings(RenderSettings(loudness_lufs=0))
    assert dialog.settings().loudness_lufs == 0.0
    dialog.set_settings(RenderSettings(loudness_lufs=-16))
    assert dialog.settings().loudness_lufs == -16.0
    dialog.close()

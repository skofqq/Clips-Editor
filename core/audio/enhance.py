"""Voice cleanup and loudness normalisation, done by FFmpeg filters.

* Noise reduction: ``afftdn`` (FFT denoiser) plus a gentle high-pass against desk rumble. It is
  a per-clip edit stored in the Timeline (``Timeline.denoise``).
* Loudness: two-pass ``loudnorm`` (EBU R128). The first pass measures the
  edited audio, the second applies a linear gain so the whole Short sits at
  the target (YouTube plays everything at about -14 LUFS) with true peaks
  under -1 dBTP. It is an export setting (``RenderSettings.loudness_lufs``).
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from core.media.ffmpeg_locator import no_window_kwargs

log = logging.getLogger(__name__)

DENOISE_OFF, DENOISE_LIGHT, DENOISE_MEDIUM, DENOISE_STRONG = 0, 1, 2, 3
DENOISE_LEVELS = (DENOISE_OFF, DENOISE_LIGHT, DENOISE_MEDIUM, DENOISE_STRONG)
DENOISE_NAMES = {DENOISE_OFF: "Off", DENOISE_LIGHT: "Light", DENOISE_MEDIUM: "Medium", DENOISE_STRONG: "Strong"}
_DENOISE_FILTERS = {
    # nf is the per-band level treated as noise; -25..-30 catches fans and
    # hiss under a voice (measured: -24 dB of white noise at "Strong").
    DENOISE_LIGHT: "highpass=f=60,afftdn=nr=10:nf=-30",
    DENOISE_MEDIUM: "highpass=f=80,afftdn=nr=16:nf=-28",
    DENOISE_STRONG: "highpass=f=100,afftdn=nr=24:nf=-25",
}

LOUDNESS_OFF = 0.0
LOUDNESS_CHOICES = (  # (target LUFS, label)
    (-14.0, "-14 LUFS (YouTube, TikTok, Instagram)"),
    (-16.0, "-16 LUFS (a little quieter)"),
    (-23.0, "-23 LUFS (EBU broadcast)"),
)
TRUE_PEAK_DB = -1.0
LRA = 11.0
SILENT_LUFS = -70.0  # quieter than this: nothing to normalise


def denoise_filter(level: int) -> str:
    """FFmpeg filter chain for a noise reduction level ("" when off)."""
    return _DENOISE_FILTERS.get(int(level), "")


@dataclass(frozen=True)
class Loudness:
    """First-pass measurement of ``loudnorm``."""

    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float

    @property
    def silent(self) -> bool:
        return not math.isfinite(self.input_i) or self.input_i < SILENT_LUFS


def measure_filter(target_lufs: float) -> str:
    return f"loudnorm=I={target_lufs:.1f}:TP={TRUE_PEAK_DB:.1f}:LRA={LRA:.1f}:print_format=json"


def apply_filter(target_lufs: float, measured: Loudness | None) -> str:
    """Second pass. Without a measurement loudnorm works in one (dynamic) pass."""
    base = f"loudnorm=I={target_lufs:.1f}:TP={TRUE_PEAK_DB:.1f}:LRA={LRA:.1f}"
    if measured is not None:
        base += (
            f":measured_I={measured.input_i:.2f}:measured_TP={measured.input_tp:.2f}"
            f":measured_LRA={measured.input_lra:.2f}:measured_thresh={measured.input_thresh:.2f}"
            f":offset={measured.target_offset:.2f}:linear=true"
        )
    # loudnorm works at 192 kHz internally; bring it back to the output rate.
    return base + ":print_format=none,aresample=48000"


def parse_loudnorm(stderr: str) -> Loudness | None:
    """The JSON block loudnorm prints at the end of the first pass."""
    blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr)
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])

        def num(key: str) -> float:
            return float(str(data[key]).strip())

        return Loudness(num("input_i"), num("input_tp"), num("input_lra"), num("input_thresh"),
                        num("target_offset"))
    except (KeyError, ValueError):
        return None


class MeasureCancelled(Exception):
    pass


def measure_loudness(
    ffmpeg: Path,
    source: Path,
    filter_complex: str,
    label: str,
    workdir: Path,
    cancel: threading.Event | None = None,
    script_option: str = "-filter_complex_script",
) -> Loudness | None:
    """Run the first pass over ``filter_complex`` (which must end in
    ``measure_filter`` at ``[label]``). None when it cannot be measured."""
    script = workdir / "loudness.txt"
    script.write_text(filter_complex, encoding="utf-8")
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(source), "-vn", script_option, str(script),
           "-map", f"[{label}]", "-f", "null", "-"]
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                               encoding="utf-8", errors="replace", **no_window_kwargs())
    chunks: list[str] = []
    reader = threading.Thread(target=lambda: chunks.append(process.stderr.read()), daemon=True)
    reader.start()
    while process.poll() is None:
        if cancel is not None and cancel.wait(0.1):
            process.kill()
            process.wait()
            raise MeasureCancelled("Loudness measurement cancelled")
        if cancel is None:
            reader.join(0.1)
    reader.join(5)
    stderr = "".join(chunks)
    if process.returncode != 0:
        log.warning("Loudness measurement failed (%s): %s", process.returncode, stderr[-800:])
        return None
    return parse_loudnorm(stderr)


def measure_file(ffmpeg: Path, path: Path, target_lufs: float = -14.0,
                 cancel: threading.Event | None = None, timeout: float = 600.0) -> Loudness | None:
    """Loudness of a finished file's first audio track (integrated LUFS and
    true peak), to check what the render really produced. None when it has no
    audio or cannot be read."""
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:a:0", "-vn", "-sn",
           "-af", measure_filter(target_lufs or -14.0), "-f", "null", "-"]
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", errors="replace", **no_window_kwargs())
    except OSError as exc:
        log.warning("Loudness check could not start: %s", exc)
        return None
    chunks: list[str] = []
    reader = threading.Thread(target=lambda: chunks.append(process.stderr.read()), daemon=True)
    reader.start()
    waited = 0.0
    while process.poll() is None:
        if (cancel is not None and cancel.is_set()) or waited > timeout:
            process.kill()
            process.wait()
            return None
        reader.join(0.1)
        waited += 0.1
    reader.join(5)
    if process.returncode != 0:
        return None
    return parse_loudnorm("".join(chunks))


def loudness_report(measured: Loudness | None, target_lufs: float) -> str:
    """One line for the render summary: what the finished file measures, with a
    hint when it is off the target (more than 1 LU) or peaks above -1 dBTP."""
    if measured is None or not target_lufs:
        return ""
    if measured.silent:
        return "Loudness: the sound is silent."
    text = f"Loudness {measured.input_i:.1f} LUFS, peak {measured.input_tp:.1f} dBTP (target {target_lufs:.0f} LUFS)"
    notes = []
    if abs(measured.input_i - target_lufs) > 1.0:
        notes.append("quieter than the target: loud peaks left no room" if measured.input_i < target_lufs
                     else "louder than the target")
    if measured.input_tp > TRUE_PEAK_DB + 0.5:
        notes.append("peaks above -1 dB")
    return text + (" · " + ", ".join(notes) if notes else " ✓")


def render_preview(
    ffmpeg: Path,
    source: Path,
    start_s: float,
    length_s: float,
    output: Path,
    denoise: int = 0,
    gain_db: float = 0.0,
    loudness_lufs: float = LOUDNESS_OFF,
    timeout: float = 120.0,
) -> Path:
    """A short WAV of the source audio with the cleanup applied, to listen
    to before exporting (cuts and mutes are not applied here)."""
    chain = ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    if denoise_filter(denoise):
        chain.append(denoise_filter(denoise))
    if gain_db:
        chain.append(f"volume={gain_db:.2f}dB")
    if loudness_lufs:
        chain.append(apply_filter(loudness_lufs, None))
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-ss", f"{max(0.0, start_s):.3f}", "-t", f"{max(0.5, length_s):.3f}", "-i", str(source),
           "-vn", "-af", ",".join(chain), "-c:a", "pcm_s16le", str(output)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=timeout, **no_window_kwargs())
    if result.returncode != 0 or not output.is_file():
        raise RuntimeError(result.stderr.strip()[-500:] or "FFmpeg failed")
    return output

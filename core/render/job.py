"""Run FFmpeg for a render: progress, cancel, GPU→CPU fallback, validation."""

from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.editor.composition import Composition
from core.media.errors import MediaError
from core.media.ffmpeg_locator import no_window_kwargs, tool_version
from core.media.probe import MediaInfo, probe_media
from core.render.encoders import SOFTWARE_ENCODER, EncoderInfo, choose_encoder, encoder_info, video_args
from core.render.graph import RenderGraph, build_graph
from core.render.settings import AAC, AUDIO_SAMPLE_RATE, HEVC, MP4, RenderSettings
from core.timeline.model import Timeline

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]


class RenderError(RuntimeError):
    pass


class RenderCancelled(Exception):
    pass


@dataclass(frozen=True)
class RenderProgress:
    fraction: float
    fps: float
    speed: float
    eta_s: float | None
    encoder: str

    def message(self) -> str:
        parts = [f"{self.fraction * 100:.0f}%"]
        if self.fps > 0:
            parts.append(f"{self.fps:.0f} fps")
        if self.eta_s is not None:
            m, s = divmod(int(round(self.eta_s)), 60)
            parts.append(f"{m}:{s:02d} left")
        parts.append(self.encoder)
        return " · ".join(parts)


@dataclass(frozen=True)
class RenderResult:
    path: Path
    encoder: str
    duration_s: float
    size_bytes: int
    elapsed_s: float
    notice: str  # e.g. why the CPU was used instead of the GPU
    info: MediaInfo


# ------------------------------------------------------------------ helpers

def _ffmpeg_major(ffmpeg: Path) -> int:
    """Major version; development builds ("N-…", "git-…") count as new."""
    line = tool_version(ffmpeg)
    match = re.search(r"version\s+n?(\d+)\.", line)
    if match:
        return int(match.group(1))
    return 99 if "version" in line else 0


def _filter_args(graph: RenderGraph, ffmpeg: Path, workdir: Path) -> list[str]:
    script = workdir / "filter.txt"
    script.write_text(graph.filter_complex, encoding="utf-8")
    # A script file avoids the Windows command-line length limit.
    if _ffmpeg_major(ffmpeg) >= 7:
        return ["-/filter_complex", str(script)]
    return ["-filter_complex_script", str(script)]


def build_command(
    ffmpeg: Path,
    source: Path,
    output: Path,
    graph: RenderGraph,
    settings: RenderSettings,
    encoder: str,
    filter_args: list[str],
    overlay: Path | None = None,
) -> list[str]:
    cmd = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-progress", "pipe:1", "-stats_period", "0.5",
        "-i", str(source),
        *(["-f", "concat", "-safe", "0", "-i", str(overlay)] if overlay is not None else []),
        *filter_args,
        "-map", f"[{graph.video_label}]",
    ]
    cmd += video_args(encoder, settings, float(graph.fps))
    if encoder_info(encoder).codec == HEVC and settings.container == MP4:
        cmd += ["-tag:v", "hvc1"]  # plays in QuickTime and browsers
    if graph.audio_label:
        cmd += ["-map", f"[{graph.audio_label}]"]
        cmd += ["-c:a", "aac" if settings.audio_codec == AAC else "libopus"]
        cmd += ["-b:a", f"{settings.audio_bitrate_k}k", "-ar", str(AUDIO_SAMPLE_RATE)]
    else:
        cmd += ["-an"]
    cmd += ["-t", f"{graph.duration_s:.6f}", "-r", str(graph.fps)]
    if settings.container == MP4:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-map_metadata", "-1", str(output)]
    return cmd


def _read_lines(stream, sink: "queue.Queue[str | None]") -> None:
    for line in iter(stream.readline, ""):
        sink.put(line)
    sink.put(None)


def _run_ffmpeg(
    cmd: list[str],
    duration_s: float,
    encoder: str,
    progress: Progress,
    cancel: threading.Event | None,
    stderr_path: Path,
) -> None:
    log.info("Render: %s", " ".join(cmd))
    with stderr_path.open("w", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", **no_window_kwargs(),
        )
        lines: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(target=_read_lines, args=(proc.stdout, lines), daemon=True)
        reader.start()
        started = time.monotonic()
        values: dict[str, str] = {}
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise RenderCancelled()
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    if proc.poll() is not None and not reader.is_alive():
                        break
                    continue
                if line is None:
                    break
                key, _, value = line.strip().partition("=")
                values[key] = value
                if key != "progress":
                    continue
                done_us = _int(values.get("out_time_us") or values.get("out_time_ms"))
                fraction = min(1.0, max(0.0, done_us / 1e6 / duration_s)) if duration_s > 0 else 0.0
                elapsed = time.monotonic() - started
                eta = elapsed * (1 - fraction) / fraction if fraction > 0.01 else None
                info = RenderProgress(
                    fraction, _float(values.get("fps")), _float((values.get("speed") or "").rstrip("x")),
                    eta, encoder,
                )
                progress(min(fraction, 0.99), info.message())
            proc.wait()
        except BaseException:
            _stop(proc)
            raise
    if proc.returncode != 0:
        tail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()[-1500:]
        raise RenderError(f"FFmpeg failed (exit code {proc.returncode}).\n{tail}")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _float(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def validate_output(
    path: Path, ffprobe: Path, settings: RenderSettings, expected_s: float, has_audio: bool
) -> MediaInfo:
    """Basic checks that the file is a playable render of what was asked."""
    if not path.is_file() or path.stat().st_size == 0:
        raise RenderError("FFmpeg did not write an output file.")
    try:
        info = probe_media(path, ffprobe)
    except MediaError as exc:
        raise RenderError(f"The rendered file cannot be read: {exc}") from exc
    video = info.video
    if video is None:
        raise RenderError("The rendered file has no video stream.")
    if video.codec != settings.video_codec:
        raise RenderError(f"Expected {settings.video_codec} video, got {video.codec}.")
    if (video.width, video.height) != (settings.width, settings.height):
        raise RenderError(
            f"Expected {settings.width}×{settings.height}, got {video.width}×{video.height}."
        )
    tolerance = max(0.5, expected_s * 0.03)
    if abs(info.duration_s - expected_s) > tolerance:
        raise RenderError(f"Expected {expected_s:.2f} s, the file is {info.duration_s:.2f} s long.")
    if has_audio:
        if not info.audio:
            raise RenderError("The rendered file has no audio stream.")
        if info.audio[0].codec != settings.audio_codec:
            raise RenderError(f"Expected {settings.audio_codec} audio, got {info.audio[0].codec}.")
    return info


def partial_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}.rendering{output.suffix}")


# ------------------------------------------------------------------ render

def render(
    source: MediaInfo,
    composition: Composition,
    timeline: Timeline,
    settings: RenderSettings,
    output: Path,
    ffmpeg: Path,
    ffprobe: Path,
    encoders: list[EncoderInfo] | None = None,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    captions: list | None = None,
    subtitle_style=None,
) -> RenderResult:
    """Render ``source`` with the edit to ``output``.

    Writes to a temporary name next to the output and renames it only after
    FFmpeg succeeded and the file passed validation. If a hardware encoder
    fails, the render is retried once with the software encoder.
    """
    progress = progress or (lambda f, m="": None)
    settings = settings.cleaned()
    output = Path(output)
    if output.suffix.lower() != settings.extension:
        output = output.with_suffix(settings.extension)
    if output.resolve() == Path(source.path).resolve():
        raise RenderError("The output must not overwrite the source video.")
    output.parent.mkdir(parents=True, exist_ok=True)
    burn = bool(captions) and subtitle_style is not None and subtitle_style.enabled
    graph = build_graph(composition, timeline, source, settings, overlay_input=1 if burn else None)
    encoder = choose_encoder(settings, encoders)
    partial = partial_path(output)
    started = time.monotonic()
    notice = ""
    with tempfile.TemporaryDirectory(prefix="tss-render-") as tmp:
        workdir = Path(tmp)
        filter_args = _filter_args(graph, ffmpeg, workdir)
        overlay = None
        if burn:
            from core.subtitles.raster import render_overlay

            progress(0.0, "Drawing subtitles…")
            overlay = render_overlay(
                captions, subtitle_style, composition.output_w, composition.output_h, workdir / "subs",
                divider=composition.divider, duration_us=timeline.output_us, cancel=cancel,
            )
            if overlay is None and cancel is not None and cancel.is_set():
                raise RenderCancelled("Render cancelled")
            if overlay is None:
                graph = build_graph(composition, timeline, source, settings)
                filter_args = _filter_args(graph, ffmpeg, workdir)
        attempts = [encoder]
        software = SOFTWARE_ENCODER[settings.video_codec]
        if encoder != software:
            attempts.append(software)
        try:
            for index, name in enumerate(attempts):
                label = encoder_info(name).label
                progress(0.0, f"Starting · {label}")
                cmd = build_command(ffmpeg, Path(source.path), partial, graph, settings, name, filter_args,
                                    overlay)
                try:
                    _run_ffmpeg(cmd, graph.duration_s, label, progress, cancel, workdir / "stderr.txt")
                    encoder = name
                    break
                except RenderError as exc:
                    if index + 1 == len(attempts):
                        raise
                    log.warning("Encoder %s failed, retrying on the CPU: %s", name, exc)
                    notice = f"{label} failed, rendered on the CPU instead."
            progress(0.995, "Checking the file…")
            info = validate_output(partial, ffprobe, settings, graph.duration_s, graph.audio_label is not None)
            os.replace(partial, output)
        except BaseException:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    size = output.stat().st_size
    progress(1.0, "Done")
    return RenderResult(
        path=output,
        encoder=encoder,
        duration_s=info.duration_s,
        size_bytes=size,
        elapsed_s=time.monotonic() - started,
        notice=notice,
        info=info,
    )

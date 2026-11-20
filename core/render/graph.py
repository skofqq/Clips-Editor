"""FFmpeg filter graph for the final video.

The graph is built from the same models the preview uses, so what you
see is what you get:

* video: kept segments (source time) are trimmed and joined, converted to
  the output frame rate, then each Stacked Layout region is cropped,
  scaled and placed on a black canvas exactly as ``Composition.layout``
  says (the preview paints the same placements);
* audio: mute regions are applied in source time with sample accuracy
  (Silence, or a sine Beep), then the kept segments are joined, and gain
  and Fade In / Fade Out are applied in output time, like the preview.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable
from fractions import Fraction

from core.editor.composition import Composition
from core.editor.track import piecewise_expression
from core.media.probe import MediaInfo
from core.audio.enhance import denoise_filter, measure_filter
from core.render.settings import AUDIO_SAMPLE_RATE, SOURCE_FPS, RenderSettings
from core.timeline.model import BEEP, Timeline

BEEP_AMPLITUDE = 0.2  # ≈ -14 dBFS: clearly audible, not painful
MAX_SOURCE_FPS = 60


@dataclass(frozen=True)
class RenderGraph:
    filter_complex: str
    video_label: str
    audio_label: str | None
    duration_s: float  # length of the rendered video
    fps: Fraction


def _t(us: int) -> str:
    """Seconds with microsecond precision, as FFmpeg expects."""
    return f"{us / 1_000_000:.6f}"


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def output_fps(settings: RenderSettings, info: MediaInfo) -> Fraction:
    if settings.fps != SOURCE_FPS:
        return Fraction(settings.fps)
    video = info.video
    fps = (video.avg_fps or video.fps) if video else Fraction(30)
    if not fps or fps <= 0:
        fps = Fraction(30)
    if fps > MAX_SOURCE_FPS:
        fps = Fraction(MAX_SOURCE_FPS)
    return fps.limit_denominator(1001)


def _fps_str(fps: Fraction) -> str:
    return f"{fps.numerator}/{fps.denominator}" if fps.denominator != 1 else str(fps.numerator)


def _mute_expression(timeline: Timeline, channels: int, beep_hz: int) -> str | None:
    """``aeval`` expressions: silence (and beep) inside mute regions, per sample."""
    merged = timeline.merged_mutes()
    if not merged:
        return None
    muted = "+".join(f"between(t,{_t(a)},{_t(b)})" for a, b, _ in merged)
    beeps = [f"between(t,{_t(a)},{_t(b)})" for a, b, mode in merged if mode == BEEP]
    keep = f"(1-min(1,{muted}))"
    tone = f"+{BEEP_AMPLITUDE}*sin(2*PI*{beep_hz}*t)*min(1,{'+'.join(beeps)})" if beeps else ""
    return "|".join(f"val({ch})*{keep}{tone}" for ch in range(channels))


def _place(lines: list[str], source: str, composition: Composition, src_w: int, src_h: int, fps: Fraction,
           tag: str, out_label: str, to_time: Callable[[int], float] | None = None) -> None:
    """Crop, scale and place the Stacked Layout regions of ``source`` on a black canvas.

    ``to_time`` maps source microseconds to the filter's ``t`` (seconds);
    tracked crops (face following) move with an expression of ``t``."""
    canvas_w, canvas_h = composition.output_w, composition.output_h
    placements = composition.layout(src_w, src_h)
    lines.append(f"color=c=black:s={canvas_w}x{canvas_h}:r={_fps_str(fps)},format=yuv420p[{tag}bg]")
    if not placements:
        # No region enabled: show the whole frame, letterboxed.
        lines.append(
            f"[{source}]scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,setsar=1[{tag}full]"
        )
        lines.append(f"[{tag}bg][{tag}full]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1[{out_label}]")
        return
    k = len(placements)
    lines.append(f"[{source}]split={k}" + "".join(f"[{tag}vp{i}]" for i in range(k)) if k > 1
                 else f"[{source}]null[{tag}vp0]")
    current = f"{tag}bg"
    for i, p in enumerate(placements):
        x = max(0, min(src_w - 2, int(round(p.src.x))))
        y = max(0, min(src_h - 2, int(round(p.src.y))))
        w = max(2, min(src_w - x, int(round(p.src.w))))
        h = max(2, min(src_h - y, int(round(p.src.h))))
        dw, dh = _even(p.dst.w), _even(p.dst.h)
        dx, dy = int(round(p.dst.x)), int(round(p.dst.y))
        region = composition.region(p.name)
        if region.track and to_time is not None:
            x_expr, y_expr = _track_expressions(region, x, y, w, h, src_w, src_h, to_time)
            crop = f"crop=w={w}:h={h}:x='{x_expr}':y='{y_expr}'"
        else:
            crop = f"crop={w}:{h}:{x}:{y}"
        lines.append(f"[{tag}vp{i}]{crop},scale={dw}:{dh}:flags=lanczos,setsar=1[{tag}region{i}]")
        out = out_label if i == k - 1 else f"{tag}layer{i}"
        lines.append(f"[{current}][{tag}region{i}]overlay=x={dx}:y={dy}:shortest=1[{out}]")
        current = out


def _track_expressions(region, x: int, y: int, w: int, h: int, src_w: int, src_h: int,
                       to_time: Callable[[int], float]) -> tuple[str, str]:
    xs, ys = [], []
    for key in region.track:
        moved = region.at(key.t_us).crop
        t = to_time(key.t_us)
        xs.append((t, x + (moved.x - region.crop.x) * src_w))
        ys.append((t, y + (moved.y - region.crop.y) * src_h))
    x_expr = f"clip({piecewise_expression(xs)},0,{max(0, src_w - w)})"
    y_expr = f"clip({piecewise_expression(ys)},0,{max(0, src_h - h)})"
    return x_expr, y_expr


def build_graph(
    composition: Composition,
    timeline: Timeline,
    info: MediaInfo,
    settings: RenderSettings,
    overlay_input: int | None = None,
    loudnorm: str = "",
) -> RenderGraph:
    """``loudnorm``: loudness filter put at the very end of the audio chain.
    ``overlay_input``: index of an extra FFmpeg input (a transparent image
    stream the size of the canvas, e.g. burnt-in subtitles) laid over the frame."""
    if info.video is None:
        raise ValueError("The source has no video stream.")
    settings = settings.cleaned()
    fps = output_fps(settings, info)
    src_w, src_h = info.video.display_size
    canvas_w, canvas_h = composition.output_w, composition.output_h
    segments = timeline.segments
    n = len(segments)
    lines: list[str] = []

    # ---------------------------------------------------------------- video
    head = f"[0:v:0]scale={src_w}:{src_h},setsar=1"
    if n == 1:
        lines.append(f"{head}[vsrc0]")
    else:
        lines.append(f"{head},split={n}" + "".join(f"[vsrc{i}]" for i in range(n)))
    for i, seg in enumerate(segments):
        lines.append(f"[vsrc{i}]trim=start={_t(seg.start_us)}:end={_t(seg.end_us)},setpts=PTS-STARTPTS[vseg{i}]")
    if not timeline.has_layouts:
        if n == 1:
            lines.append(f"[vseg0]fps={_fps_str(fps)}[vcut]")
        else:
            lines.append("".join(f"[vseg{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0,fps={_fps_str(fps)}[vcut]")
        _place(lines, "vcut", composition, src_w, src_h, fps, "", "canvas",
               lambda us: timeline.source_to_output(us) / 1_000_000)
    else:
        # Parts with their own layout (the webcam moved mid-clip): lay out every
        # part on its own canvas, then join the canvases.
        for i, seg in enumerate(segments):
            layout = seg.layout if seg.layout is not None else composition
            layout = replace(layout, output_w=canvas_w, output_h=canvas_h)
            lines.append(f"[vseg{i}]fps={_fps_str(fps)}[vfps{i}]")
            _place(lines, f"vfps{i}", layout, src_w, src_h, fps, f"p{i}_", f"canvas{i}",
                   lambda us, start=seg.start_us: (us - start) / 1_000_000)
        if n == 1:
            lines.append("[canvas0]null[canvas]")
        else:
            lines.append("".join(f"[canvas{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[canvas]")
    if overlay_input is not None:
        lines.append(f"[{overlay_input}:v]format=rgba,setpts=PTS-STARTPTS[subs]")
        lines.append("[canvas][subs]overlay=x=0:y=0:eof_action=pass:repeatlast=0:format=auto[canvas_s]")
        lines.append("[canvas_s]format=yuv420p[canvas_f]")
        canvas_label = "canvas_f"
    else:
        canvas_label = "canvas"
    if (settings.width, settings.height) == (canvas_w, canvas_h):
        lines.append(f"[{canvas_label}]format=yuv420p[vout]")
    else:
        # Other sizes / aspect ratios: fit the canvas and pad with black.
        lines.append(
            f"[{canvas_label}]scale={settings.width}:{settings.height}:force_original_aspect_ratio=decrease"
            f":flags=lanczos,pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2,"
            "setsar=1,format=yuv420p[vout]"
        )

    # ---------------------------------------------------------------- audio
    audio_label = None
    if info.audio:
        lines += audio_lines(timeline, settings, loudnorm)
        audio_label = "aout"

    return RenderGraph(
        filter_complex=";\n".join(lines),
        video_label="vout",
        audio_label=audio_label,
        duration_s=timeline.output_us / 1_000_000,
        fps=fps,
    )


def audio_lines(timeline: Timeline, settings: RenderSettings, loudnorm: str = "") -> list[str]:
    """Audio part of the graph, from ``[0:a:0]`` to ``[aout]``: noise
    reduction, mutes (source time), cuts, then gain, fades and loudness."""
    lines: list[str] = []
    segments = timeline.segments
    n = len(segments)
    chain = (
        f"[0:a:0]aresample={AUDIO_SAMPLE_RATE},"
        "aformat=sample_fmts=fltp:channel_layouts=stereo"
    )
    cleanup = denoise_filter(timeline.denoise)
    if cleanup:
        chain += f",{cleanup}"
    expression = _mute_expression(timeline, 2, settings.beep_hz)
    if expression:
        chain += f",aeval=exprs='{expression}':channel_layout=stereo"
        chain += ",aformat=sample_fmts=fltp:channel_layouts=stereo"
    if n == 1:
        lines.append(f"{chain}[asrc0]")
    else:
        lines.append(f"{chain},asplit={n}" + "".join(f"[asrc{i}]" for i in range(n)))
    for i, seg in enumerate(segments):
        lines.append(f"[asrc{i}]atrim=start={_t(seg.start_us)}:end={_t(seg.end_us)},asetpts=PTS-STARTPTS[aseg{i}]")
    post = []
    if timeline.gain_db:
        post.append(f"volume={timeline.gain_db:.2f}dB")
    if loudnorm:
        # Normalise before the fades so a fade-out does not lower the measured level.
        post.append(loudnorm)
    out_s = timeline.output_us / 1_000_000
    if timeline.fade_in_us > 0:
        post.append(f"afade=t=in:st=0:d={_t(timeline.fade_in_us)}")
    if timeline.fade_out_us > 0:
        start = max(0.0, out_s - timeline.fade_out_us / 1_000_000)
        post.append(f"afade=t=out:st={start:.6f}:d={_t(timeline.fade_out_us)}")
    tail = ("," + ",".join(post)) if post else ""
    if n == 1:
        lines.append(f"[aseg0]anull{tail}[aout]")
    else:
        lines.append("".join(f"[aseg{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1{tail}[aout]")
    return lines


def loudness_graph(timeline: Timeline, settings: RenderSettings) -> str:
    """Audio-only graph for the first loudness pass (ends in ``[aout]``)."""
    return ";\n".join(audio_lines(timeline, settings, measure_filter(settings.loudness_lufs)))

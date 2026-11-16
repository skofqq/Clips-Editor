# Twitch Shorts Studio — Technical Architecture

This document answers section 18 of the PRD
(`docs/Twitch_Shorts_Studio_Full_Development_Plan.md`): architecture,
dependencies and risks, module interfaces, and the phased checklist.

## 1. Layering

```
ui/  ──────────►  core/  ──────────►  external tools (FFmpeg, faster-whisper, YouTube API)
 │                  ▲
 └──► app/ (bootstrap: config, logging, QApplication, DI of services)
database/ is used only by core/ repositories, never directly by ui/.
```

Rules:

* `core/` has **no Qt Widgets imports**. It may use `QtCore` only for
  background workers and signals (`core/workers.py`). Pure logic
  (probe parsing, time math, composition geometry, filter-graph building,
  censor matching) is plain Python and unit-tested without a GUI.
* `ui/` talks to `core/` through small service objects and dataclasses.
  UI never builds FFmpeg command lines.
* Every long operation (probe, waveform, thumbnails, transcription,
  render, upload) runs in a worker (`QThreadPool` + `QRunnable`) and
  reports back through Qt signals. The GUI thread never waits.
* Source media is opened read-only. Everything generated (cache,
  proxies, waveforms, renders) goes to the cache/output directories.

## 2. Directory layout

```
app/            main.py (entry), bootstrap.py (QApplication, theme), config.py, logging_setup.py
ui/
  main_window/  MainWindow: docks, menus, actions
  panels/       MediaPanel, PropertiesPanel, TimelinePanel (stub in Phase 1)
  preview/      PreviewPanel, VideoCanvas (frame painter; crop overlays in Phase 2)
  theme/        dark.qss
core/
  workers.py    Worker/WorkerSignals wrapper around QThreadPool
  media/        ffmpeg_locator.py, probe.py (MediaInfo), errors.py
  projects/     Project model, .tssproj JSON (de)serialisation, schema versioning
  editor/       (Phase 2) composition geometry, presets
  timeline/     (Phase 3) rational time, tracks, edit model, undo stack
  audio/        (Phase 3) waveform extraction/cache, mute regions, fades
  transcription/(Phase 4) faster-whisper + Silero VAD
  censor/       (Phase 5) dictionaries, normaliser, matcher, region merger
  render/       (Phase 6) filter-graph builder, encoder detection, render job
  metadata/     (Phase 8) templates, chips libraries
  publishing/youtube/ (Phase 7) OAuth, channels, resumable upload
database/       (Phase 8) SQLite models, repositories, migrations
tools/          setup_ffmpeg.py — downloads a Windows FFmpeg build into tools/ffmpeg/
tests/          pytest; GUI smoke tests run with QT_QPA_PLATFORM=offscreen
```

## 3. Key interfaces (stable contracts between modules)

```python
# core/media
locate_ffmpeg(config) -> FFmpegPaths            # raises FFmpegNotFoundError
probe_media(path, ffprobe) -> MediaInfo         # raises ProbeError, never mutates source
MediaInfo: path, size_bytes, duration_s, container, video: VideoStream|None,
           audio: list[AudioStream]
VideoStream: codec, width, height, fps (Fraction), time_base, pix_fmt,
             rotation, is_vfr_suspected, display_width/height
AudioStream: index, codec, sample_rate, channels, channel_layout, language

# core/projects
Project: schema_version, name, media: list[MediaRef], composition, timeline,
         audio_edits, transcript, censor, render_settings, metadata
save_project(project, path) / load_project(path) -> Project   # .tssproj = UTF-8 JSON

# core/editor (implemented in Phase 2)
Region(crop: Rect normalised 0..1, enabled, fit: FILL|FIT, scale, offset_x, offset_y)
Composition(output_w, output_h, divider, swapped, webcam: Region, gameplay: Region)  # immutable
Composition.layout(source_w, source_h) -> list[Placement(name, slot, src, dst)]
#   src = visible source pixels, dst = output pixels. Maps 1:1 to FFmpeg
#   crop=src → scale=dst.size → overlay at dst.pos.
# The same layout is consumed by the preview painter (ui/preview/output_view.py)
# AND the Phase 6 filter-graph builder, so preview == render by construction.
PresetStore(path): built-in + user presets (presets.json in the user data dir)
# ui/editor/CompositionController: single shared editing state (Qt signals)
#   used by SourceView, OutputView and CompositionPanel.

# core/timeline (Phase 3)
Times stored as integer microseconds or Fraction seconds; frame/sample
positions derived on demand to avoid rounding drift.

# core/render (Phase 6)
detect_encoders(ffmpeg) -> list[EncoderInfo]   # h264_nvenc, hevc_nvenc, h264_qsv, h264_amf, libx264, libx265
build_filtergraph(project, media_info) -> str
RenderJob(project, settings) -> worker with progress(fps, percent, eta), cancel()
```

## 4. Preview strategy

* Phase 1 uses Qt Multimedia (`QMediaPlayer` + `QVideoSink`). On Windows,
  Qt 6.5+ ships an FFmpeg-based backend, so MP4/MKV/WebM play without
  system codecs. Frames are painted by our own `VideoCanvas`, not by
  `QVideoWidget`, so Phase 2 can draw crop rectangles and the composed
  9:16 output on top of the same frames.
* Final output always comes from the FFmpeg render path, and the
  preview reuses the same composition geometry function.

## 5. Dependencies and compatibility risks

| Risk | Mitigation |
|---|---|
| FFmpeg not installed on the user's PC | `tools/setup_ffmpeg.py` downloads a GPL build into `tools/ffmpeg/`; locator checks config → bundled → PATH. |
| NVENC availability depends on driver version | Probe encoders with a 1-frame test encode at startup (Phase 6) and fall back to libx264. |
| Variable frame rate Twitch clips | Probe compares `r_frame_rate` vs `avg_frame_rate`; render with explicit `-r`/`fps` filter. |
| faster-whisper CUDA needs cuBLAS/cuDNN | CPU path always available; GPU optional with clear diagnostics (Phase 4). |
| Qt Multimedia backend differences | Preview is advisory; render is authoritative; fall back to FFmpeg frame extraction if needed. |
| Nuitka + PySide6 + CTranslate2 packaging size | Keep models outside the bundle, downloaded on first run (Phase 9). |
| YouTube API quota / OAuth verification | Use minimal scopes, private-by-default uploads (Phase 7). |

## 6. Phase checklist

1. **Foundation** — config, logging, FFmpeg locator, ffprobe import, dark
   main window with 4 resizable docks, original preview with play/seek,
   tests. *(this phase)*
2. **Vertical editor** — 9:16 canvas, webcam/gameplay regions with mouse
   handles, divider, fit/fill, swap, presets, `.tssproj` persistence.
3. **Timeline & audio** — timeline widget, waveform cache, trim/split,
   mute regions, gain, fades, undo/redo.
4. **Transcription** — faster-whisper, VAD, Transcript panel.
5. **Censor** — dictionaries, normaliser, suggestions, review panel.
6. **Rendering** — filter graph, encoder detection, NVENC/QSV/AMF/CPU,
   progress/ETA/cancel, output validation.
7. **YouTube** — OAuth, channels, resumable upload.
8. **Metadata manager** — SQLite libraries, chips, templates.
9. **Packaging & QA** — Nuitka build, first-run setup, clean-machine test.

Per the PRD priorities, rendering (6) may be pulled ahead of 4–5 once
Phase 3 is stable, so the editor is usable end-to-end before AI work.

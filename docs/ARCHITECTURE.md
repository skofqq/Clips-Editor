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

# core/timeline (implemented in Phase 3)
# All positions are integer microseconds in SOURCE time; frames and audio
# samples are derived with rational maths (timebase.py), so nothing drifts.
Timeline(duration_us, segments: tuple[Segment], mutes: tuple[MuteRegion],
         gain_db, fade_in_us, fade_out_us)            # immutable
#   segments = kept source ranges (trim / split / cut only edit this list)
#   mutes    = source-time ranges, mode "silence" | "beep"; transcript and
#              censor results (Phases 4-5) map onto them without conversion
Timeline.merged_mutes()  -> ranges for preview/render (originals stay editable)
Timeline.volume_at(us)   -> linear multiplier (mute, gain, fades)
Timeline.source_to_output(us) / output_to_source(us)
# core/history.History: snapshot undo/redo over the editor state tuple;
#   a drag is folded into one step.
# core/audio/waveform: FFmpeg -> 8 kHz mono -> 100 peaks/s, cached in
#   <data dir>/cache/waveforms keyed by path+size+mtime.
# ui/timeline/TimelineController + TimelineView; ui/preview/EditPlayback
#   makes the preview skip cuts and follow mutes/gain/fades.
# Project file: "timelines": one entry per media path.

# core/transcription (implemented in Phase 4)
Transcript(language, language_probability, model, segments: tuple[Segment])   # immutable
Segment(id, start_us, end_us, text, words: tuple[Word], edited)
Word(start_us, end_us, text, probability, timing: "word" | "segment")
#   "segment" timing = no reliable word timestamp (model gave none, or the user
#   changed the number of words). Censor (Phase 5) must treat it as approximate.
WhisperEngine(models_dir).transcribe(audio_16k_float32, TranscribeOptions, progress, cancel)
transcribe_file(engine, path, ffmpeg, options, progress, cancel)   # FFmpeg decode + engine
#   faster-whisper with vad_filter (Silero VAD) and word_timestamps; CUDA float16
#   when available, CPU int8 otherwise, CPU retry if CUDA fails at runtime.
# core/workers.CancellableTask: QRunnable with progress + cancel for long jobs.
# Project file: "transcripts": one entry per media path.

# core/censor (implemented in Phase 5)
# normalize.forms(word): canonical + look-alike/digit/transliterated spellings,
#   each also with stretched letters collapsed. Same normalisation for entries.
# dictionary.Dictionary(custom_words, exclusions).match(word, categories)
#   built-in regex roots (ru/en, strong/mild) + user entries (word, word*, *word*),
#   exceptions per root ("хлеб" for е-б); <data dir>/censor_dictionary.json.
# review.detect(transcript, dictionary, CensorSettings, previous) -> CensorState
#   with PENDING Suggestions (padded ranges; exact=False for "segment" timing).
#   Re-detection keeps statuses and hand-adjusted ranges of the same word.
# CensorState.apply(timeline) -> (timeline, state): replaces the censor-owned
#   mutes (applied_mute_ids) with merged ACCEPTED ranges. Nothing else mutes.
# Undo history state is (Composition, Timeline, Transcript, CensorState);
#   Apply changes timeline + censor in one step.
# Project file: "censor": one entry per media path.

# core/render (implemented in Phase 6)
RenderSettings(width, height, fps (0 = source, max 60), video_codec h264|hevc,
               container mp4|mkv, audio_codec aac|opus, rate_control quality|bitrate,
               quality (CRF/CQ), video_bitrate_k, encoder auto|cpu|<name>, beep_hz); PRESETS
detect_encoders(ffmpeg) -> [EncoderInfo]   # listed in -encoders AND a 3-frame test encode works
choose_encoder(settings, encoders): NVENC > QSV > AMF > libx264/libx265
build_graph(composition, timeline, media_info, settings) -> RenderGraph
#   video: trim+concat kept segments -> fps -> crop/scale every Placement of
#   Composition.layout() onto a black canvas (same placements as the preview)
#   -> optional fit+pad to another output size.
#   audio: aeval per sample in SOURCE time (silence / sine beep in mute regions)
#   -> atrim+concat -> volume (gain) -> afade in/out in OUTPUT time -> 48 kHz.
render(source, composition, timeline, settings, output, ffmpeg, ffprobe, encoders,
       progress, cancel) -> RenderResult
#   filter graph in a script file (-/filter_complex on FFmpeg 7+), -progress
#   pipe:1 for percent/fps/ETA, writes <name>.rendering.<ext>, retries once on
#   the CPU if a hardware encoder fails, validate_output() with FFprobe
#   (codec, size, duration ±3 %, audio), then renames. Cancel kills FFmpeg and
#   deletes the partial file.
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

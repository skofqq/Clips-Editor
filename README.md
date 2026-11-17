# Twitch Shorts Studio (Clips Editor)

Windows desktop editor that turns 16:9 Twitch clips into vertical 9:16
YouTube Shorts. Full requirements: `docs/Twitch_Shorts_Studio_Full_Development_Plan.md`.
Architecture and module contracts: `docs/ARCHITECTURE.md`.

## Status

**Phase 1 — Foundation** (done): dark main window with resizable Media,
Preview, Properties and Timeline panels; video import (MP4, MKV, MOV,
WebM…) with FFprobe analysis in a background thread; original preview
with play/pause, scrubbing and frame stepping; `.tssproj` save/open;
logging and settings.

**Phase 2 — Vertical editor** (done): 9:16 output preview next to the
original; independent Webcam and Gameplay crop rectangles; divider,
Fit/Fill, zoom and position per region; swap and disable regions;
composition presets; everything saved in the `.tssproj`.

**Phase 3 — Timeline and audio** (done): timeline with zoom and scroll,
cached audio waveform, trimming, splitting and cutting, multiple Mute
Regions (Silence or Beep), overall gain, Fade In / Fade Out, snapping,
undo/redo. The preview plays the edit: cuts are skipped and mutes,
gain and fades are audible. Sources are never modified.

**Phase 4 — Speech transcription** (done): local faster-whisper with
Silero VAD, Russian / English / auto-detect, word-level timestamps,
Transcript panel (click to seek, double-click to correct, search), and a
Speech track on the timeline. Words without reliable timing are marked
`≈` and never presented as exact. Runs on the GPU when CUDA is available
and falls back to the CPU otherwise.

### Speech recognition

Open the **Transcript** tab (next to Properties), pick the language and
model, press **Transcribe**. The model is downloaded once into
`%LOCALAPPDATA%\TwitchShortsStudio\models` (`small` ≈ 480 MB).
Clicking a word on the Speech track selects it on the timeline, so `M`
mutes exactly that word.

For NVIDIA GPU acceleration install the CUDA libraries once (≈ 1 GB):

double-click `install_gpu.bat` (or run `.venv\Scripts\pip install -r requirements-gpu.txt`).

Without them the app uses the CPU automatically and says so in the
Transcript panel; it checks for cuBLAS/cuDNN before touching the GPU.

### Editing on the timeline

| Action | How |
|---|---|
| Move the playhead | click the ruler or a track |
| Trim | drag the left or right edge of a segment |
| Split at the playhead | `S` |
| Select a range | drag across the audio or video track |
| Cut the selected range / delete a segment or mute | `Del` |
| Keep only the selected range | `T` |
| Mute the selected range | `M`, then pick Silence or Beep |
| Move / resize a Mute Region | drag it or its edges on the Mute track |
| Zoom / scroll | `Ctrl`+wheel / wheel |
| Undo / redo | `Ctrl+Z` / `Ctrl+Y` |

Edges snap to the playhead and to other edges; hold `Alt` to turn
snapping off. Beep regions sound silent in the preview; the tone is
added on export (Phase 6), as is gain above 0 dB.

### Editing the composition

| Where | Mouse | Result |
|---|---|---|
| Original | drag inside a rectangle | move the crop |
| Original | drag a handle | resize the crop |
| Original | Shift+drag | draw the selected crop anew |
| Output | drag the white line | change the webcam/gameplay split |
| Output | drag inside a region | reposition the picture in its slot |
| Output | mouse wheel | zoom the region |
| Output | double-click | reset position and zoom |

Exact values, Fit/Fill, swap and presets are in **Properties → Composition**.

## Run on Windows

1. Install Python 3.12+ (with "Add to PATH").
2. Double-click `run.bat`. On first start it creates `.venv`, installs
   dependencies and downloads FFmpeg into `tools\ffmpeg` if FFmpeg is not
   already on PATH.

Manual steps, if you prefer:

```bat
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python tools\setup_ffmpeg.py
.venv\Scripts\python -m app.main
```

Settings and logs live in `%LOCALAPPDATA%\TwitchShortsStudio`.

### Shortcuts

| Key | Action |
|---|---|
| Ctrl+I | Import video |
| Space | Play / pause |
| ← / → | Previous / next frame |
| Ctrl+S / Ctrl+O | Save / open project |

Videos and `.tssproj` files can also be dragged onto the window.

## Tests

```bat
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

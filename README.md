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

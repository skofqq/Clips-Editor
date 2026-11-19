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

**Phase 5 — Automatic censorship** (done): Russian and English profanity
dictionaries that catch inflections and spelling tricks (ё/е, Latin
look-alikes, digits, stretched letters, transliteration such as `blyat`,
words already bleeped as `б**дь`). Results are **suggestions**: each one
is reviewed (accept, reject, adjust start/end, listen, correct the word,
never censor this word) and only accepted ones become Mute Regions, in
Silence or Beep mode, when you press **Apply to timeline**. Nothing is ever
muted automatically.

**Phase 6 — Rendering** (done): **File → Export Video…** (`Ctrl+E`) renders
the Short with FFmpeg exactly as the preview shows it: Stacked Layout
regions, cuts, Mute Regions (Silence or a Beep tone), gain and fades.
H.264 or H.265, MP4 or MKV, AAC or Opus at 48 kHz, constant quality or a
target bitrate. Encoders are detected at startup (NVIDIA NVENC, Intel
Quick Sync, AMD AMF) and the CPU is used when none works; if the GPU
encoder fails mid-way the render is repeated on the CPU automatically.
Progress, speed, time left and Cancel are shown, the file is written under
a temporary name and only appears once FFmpeg succeeded and FFprobe
confirmed codec, size and length.

**Phase 7 — YouTube upload** (done): **Settings → Integrations → YouTube
Accounts…** connects channels with Google's OAuth 2.0 in the browser
(PKCE, loopback redirect; the app never sees the password). Refresh tokens
and the OAuth client are stored in Windows Credential Manager, never in
config or project files. **Upload to YouTube…** (`Ctrl+U`, or the button
after an export) shows the file's size, resolution, length and codecs,
lets you set title, description, tags, category, visibility, scheduled
publication, language and "made for kids", checks YouTube's limits before
sending, and uploads with the resumable protocol: progress, speed, time
left, Cancel, automatic retries after network or server errors and token
refresh. The video ID and link are shown and kept in
`youtube_uploads.json`. Setup of your own Google OAuth client:
[docs/YOUTUBE_SETUP_RU.md](docs/YOUTUBE_SETUP_RU.md).

**Phase 8 — Metadata manager** (done): a **Metadata** tab (next to
Censor) and **Settings → Metadata Library…**. Pick the streamer,
participants, game and stream types as chips, choose a template and press
**Generate metadata** to get the title, description, YouTube tags and
hashtags. Everything stays editable; generating again never overwrites
your hand edits silently (it asks whether to keep them). Upload to YouTube
starts from these values.

### Interface

The top bar holds the main actions: Import, Open, Save, Undo / Redo, and
on the right **Upload** and **Export**. Panels on the right (Properties,
Transcript, Censor, Metadata) are tabs at the top; drag a tab out to
float it, **View → Reset Layout** puts everything back.

Long forms (Composition, Export, Upload, Transcript, Censor) are split
into cards with a title each. Recognition settings fold away once a
transcript exists (click **Recognition settings** to open them again).
Empty panels say what to do next. The media list shows a thumbnail, the
length and the resolution of every video; right-click a video to remove
it from the project (the file stays on disk). Thumbnails are cached in
`%LOCALAPPDATA%\TwitchShortsStudio\cache\thumbnails`.

The look follows Microsoft's Fluent Design (Windows 11 dark): WinUI
neutrals, 4 px controls and 8 px menus, text boxes with an accent
underline when focused, pivot tabs, Fluent check boxes and sliders. The
accent colour is taken from Windows (Settings → Personalization →
Colors), so changing it there changes the app on the next start;
`TSS_ACCENT=#rrggbb` overrides it. **Settings → Appearance** switches between Dark (default),
Light and Match Windows, and turns on the Windows 11 **Mica** backdrop
(off by default; the desktop colour shows through the top bar). Both
apply after a restart, which the app offers to do right away. On Windows 11 the title bar is dark,
blends with the window and has rounded corners. Files: `ui/theme/dark.qss`
(stylesheet), `ui/theme/palette.py` (tokens, accent), `ui/theme/windows.py`
(title bar, menus), `ui/theme/icons/` (line icons tinted in code).

### Metadata

Typing a name in a chip field offers matching profiles (also by username
or alternative names such as “GTA 6”). A name that is not in the library
yet is added on the spot; double-click its chip to add links and default
tags. The library (SQLite, `metadata_library.sqlite` in the data folder) is
shared by all projects; the project file stores only which profiles you
picked and the final texts.

Templates use variables: `{streamer}`, `{streamer_username}`,
`{streamer_channel_url}`, `{streamer_twitch_url}`, `{game}`,
`{stream_type}`, `{participants}`, `{participants_links}`, `{stream_url}`,
`{clip_title}`, `{date}`, `{hashtags}`, plus your own from the
**Variables** box (`promo = https://t.me/…`). Text in `[[ … ]]` is left
out when a variable inside is empty, and a line whose variables are all
empty disappears, so `Game: {game}` never shows a bare label. Template
choice: the one picked on the tab, else the streamer's, game's or stream
type's own template, else the default (★). YouTube tags (sent as
`snippet.tags`, up to 500 characters) are collected from the streamer,
game, participants, stream types, template and the clip's extra tags
without duplicates; hashtags only go into the description.

### Exporting

Press `Ctrl+E`. **YouTube Shorts · H.264 MP4** (1080 × 1920, quality 20)
is the safe default; HEVC presets give smaller files. Lower quality
numbers mean better quality and bigger files (18–23 is a good range).
Other resolutions such as 1:1 or 16:9 scale the 9:16 layout to fit and
pad with black. The last settings and folder are remembered.

### Speech recognition

Open the **Transcript** tab (next to Properties), pick the language and
model, press **Transcribe**. The model is downloaded once into
`%LOCALAPPDATA%\TwitchShortsStudio\models` (`small` ≈ 480 MB).
Clicking a word on the Speech track selects it on the timeline, so `M`
mutes exactly that word.

Accuracy tips: list nicknames, guest names and slang in the
**Nicknames and special words** field (comma separated; remembered
between sessions), and keep **Write swear words as spoken** on, so that
profanity is transcribed literally instead of being softened.
**Expect gaming / streaming slang** primes the model with common gamer and
Twitch words (катка, рофл, клатч, GG, KEKW…). Larger
models (`large-v3`, `large-v3-turbo`) recognise Russian noticeably better.

Recognition quality options in the Transcript panel:

- **Clean up audio** (on by default): before recognition the audio is
  filtered, compressed and normalised, so shouting is not clipped and quiet
  speech is lifted. Only the copy sent to Whisper changes, not your export.
- **Speech detection**: *Sensitive* catches quiet or overlapped speech that
  *Normal* skips; *Off* sends all audio (slower, may invent text in music).
  Long silences no longer produce invented phrases.
- **Accurate (slower)**: a wider beam search, roughly 1.5–2× slower.
- **Learned words**: every word you type when correcting the transcript or
  a censor word is remembered and suggested to the model next time.
  *Forget* clears the list.
- **Re-transcribe selection**: select a range on the timeline (or put the
  playhead on a phrase) and re-recognise just those phrases with accurate
  settings, for example after adding a nickname. It can be undone.
- **Custom model…**: a folder with a faster-whisper (CTranslate2) model or
  a Hugging Face id such as `user/whisper-large-v3-ru-ct2`.

For NVIDIA GPU acceleration install the CUDA libraries once (≈ 1 GB):

double-click `install_gpu.bat` (or run `.venv\Scripts\pip install -r requirements-gpu.txt`).

Without them the app uses the CPU automatically and says so in the
Transcript panel; it checks for cuBLAS/cuDNN before touching the GPU.

### Censoring swear words

1. Transcribe the clip, then open the **Censor** tab and press
   **Find swear words**. Choose the categories (Strong: mat and slurs;
   Mild: жопа, damn…; My words), the padding (default 150 ms before and
   250 ms after each word; negative values mute only the inside of the word,
   e.g. -60 ms before keeps its first sound; at most a quarter of the word
   is uncovered on each side, and words shorter than 0.2 s are always muted
   whole) and Silence or Beep.
2. Suggestions appear in the list and as dashed yellow boxes on the Mute
   track. Select one to jump to it; `P` (or double-click) plays it with a
   little context, `A` accepts, `R` rejects and moves to the next one.
   Words marked `≈` only have approximate timing: listen and adjust
   Start/End before accepting.
3. Double-click a word to correct a misrecognition; **Never censor this
   word** adds it to your exclusions. **Dictionary…** edits your own words
   (`word`, `word*` for prefixes, `*word*` for parts of words) and exclusions,
   stored in `%LOCALAPPDATA%\TwitchShortsStudio\censor_dictionary.json`.
4. **Apply to timeline** creates merged Mute Regions for the accepted
   suggestions only (one undo step). Applying again replaces the censor's
   regions; mutes you drew yourself are never touched. **Remove censor
   mutes** takes them off again. The review is saved in the project.

### Subtitles

1. Transcribe the clip, open the **Subtitles** tab and tick **Show
   subtitles on the video**. Pick a style (Shorts bold, Karaoke green,
   One word, Clean, Boxed) and adjust font, size, colours, outline,
   shadow or box. By default the captions sit on the webcam/gameplay
   border; **Where** can move them to any height.
2. Captions show 1–3 words at a time (see **Words**, **Letters/line**,
   **Lines**); a pause or the end of a sentence starts a new one. The word
   being spoken is highlighted. Cut words disappear and words inside a mute
   region are written as `б***` (or `****`, or as spoken).
3. In the caption list, click a caption to jump to it. **Split at
   playhead** starts a new caption at the word under the playhead, **Merge
   with next** glues two captions, **Reset** undoes your splits and merges.
   They are saved in the project.
4. **Save .srt…** writes SubRip or WebVTT in the time of the edited video.
   In the Export dialog, **Burn subtitles into the video** draws them
   exactly as the preview shows them.
5. When uploading to YouTube, **Add the subtitles as YouTube captions**
   uploads them as a caption track (CC). This needs the "manage your YouTube
   videos" permission: channels connected before 0.8.7 must be reconnected.

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
| Zoom / scroll | `Ctrl`+wheel (around the mouse) / wheel, or the zoom slider |
| Zoom in / out / whole clip | `+` / `−` / `0` |
| Zoom to the selection | `Z` (range, mute region or selected segment) |
| Taller or shorter tracks | drag the lower edge of Video, Audio or Speech (in the track names or on the timeline) |
| Taller waveform | `Alt`+wheel, or the wheel over **Audio** in the track names; double-click resets |
| Undo / redo | `Ctrl+Z` / `Ctrl+Y` |

Edges snap to the playhead, to other edges and to video frames; hold
`Alt` to turn snapping off. Zoomed in far enough, the ruler shows a tick
for every frame and the toolbar shows the frame number of the playhead.
Track heights and the waveform zoom are remembered between sessions. Beep regions sound silent in the preview; the tone is
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
| Shift+← / Shift+→ | One second back / forward |
| Home / End | Start / end of the clip |
| + / − / 0 / Z | Timeline: zoom in / out / whole clip / selection |
| Ctrl+S / Ctrl+O | Save / open project |
| Ctrl+E | Export video |
| Ctrl+U | Upload a rendered video to YouTube |
| A / R / P | Censor tab: accept / reject / listen to the selected suggestion |

Videos and `.tssproj` files can also be dragged onto the window.

## Tests

```bat
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

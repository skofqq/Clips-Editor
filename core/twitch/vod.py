"""Download a part of a Twitch stream recording (``twitch.tv/videos/<id>``).

A recording is an HLS playlist of short pieces (usually 10 s each). Only
the pieces that cover the chosen range are downloaded, several at once,
then joined and remuxed into one MP4 without re-encoding. The range is
widened to whole pieces, so the file may start a few seconds earlier and
end a few seconds later than asked; :attr:`VodDownload.start_s` is the
exact place in the stream where the file starts (the chat is aligned to
it).

yt-dlp is used only to read the recording's facts and its quality
playlists (it knows how to get Twitch's access token); the pieces are
downloaded here, so progress is exact and Cancel stops at once. Half-done
downloads are kept in a ``.parts`` folder next to the file and resumed on
the next try.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.twitch.download import DownloadCancelled, TwitchError, _friendly, _ytdlp

log = logging.getLogger(__name__)

WORKERS = 6  # pieces downloaded at once
RETRIES = 4
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"

_VOD = re.compile(r"(?:twitch\.tv/(?:[A-Za-z0-9_]+/)?(?:videos?|v)/|^v?)(?P<id>\d{6,12})(?:\b|$)", re.IGNORECASE)
_T = re.compile(r"[?&#]t=(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s?)?")


# ------------------------------------------------------------------ links and times
@dataclass(frozen=True)
class VodRef:
    video_id: str
    offset_s: int = 0  # ?t=1h2m3s in the link

    @property
    def url(self) -> str:
        return f"https://www.twitch.tv/videos/{self.video_id}"


def parse_vod_link(text: str) -> VodRef | None:
    """``twitch.tv/videos/123456789`` (optionally ``?t=1h2m3s``) or a bare id; None otherwise."""
    text = (text or "").strip().strip("<>\"'")
    if not text or "/clip/" in text or "clips.twitch.tv" in text.lower():
        return None
    match = _VOD.search(text)
    if not match:
        return None
    offset = 0
    t = _T.search(text)
    if t and any(t.groupdict().values()):
        offset = int(t["h"] or 0) * 3600 + int(t["m"] or 0) * 60 + int(t["s"] or 0)
    return VodRef(match["id"], offset)


_HMS = re.compile(r"^(?:(?P<h>\d+):)?(?P<m>\d{1,3}):(?P<s>\d{1,2})(?:[.,]\d+)?$")
_UNITS = re.compile(r"^(?:(?P<h>\d+)\s*[hч])?\s*(?:(?P<m>\d+)\s*(?:m|min|м|мин))?\s*(?:(?P<s>\d+)\s*(?:s|sec|с|сек)?)?$",
                    re.IGNORECASE)


def parse_time(text: str) -> int | None:
    """Seconds from ``1:20:00``, ``80:00``, ``4800``, ``1h20m`` or ``1ч20м``; None when unreadable."""
    text = (text or "").strip().replace(" ", "")
    if not text:
        return None
    match = _HMS.match(text)
    if match:
        if int(match["s"]) >= 60 or (match["h"] is not None and int(match["m"]) >= 60):
            return None
        return int(match["h"] or 0) * 3600 + int(match["m"]) * 60 + int(match["s"])
    match = _UNITS.match(text)
    if match and any(match.groupdict().values()):
        return int(match["h"] or 0) * 3600 + int(match["m"] or 0) * 60 + int(match["s"] or 0)
    return None


def format_time(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


# ------------------------------------------------------------------ recording facts
@dataclass(frozen=True)
class VodQuality:
    name: str  # "1080p60", "720p30", "Audio only"
    url: str  # media playlist (.m3u8)
    height: int = 0
    kbps: float = 0.0  # average bitrate, for the size estimate
    source: bool = False  # the original quality the streamer sent

    @property
    def label(self) -> str:
        return f"{self.name} (source)" if self.source else self.name

    def size_mb(self, seconds: float) -> float:
        return self.kbps * 1000 / 8 * seconds / 1_048_576 if self.kbps else 0.0


@dataclass(frozen=True)
class VodInfo:
    video_id: str
    title: str = ""
    channel: str = ""  # display name
    login: str = ""
    duration_s: float = 0.0
    created: str = ""  # YYYY-MM-DD
    qualities: tuple[VodQuality, ...] = ()

    @property
    def url(self) -> str:
        return f"https://www.twitch.tv/videos/{self.video_id}"

    def quality(self, name: str = "") -> VodQuality | None:
        """The quality called ``name`` (or the nearest lower one by height); the best when empty."""
        if not self.qualities:
            return None
        if name:
            for q in self.qualities:
                if q.name.casefold() == name.casefold():
                    return q
            wanted = _height(name)
            if wanted:
                lower = [q for q in self.qualities if 0 < q.height <= wanted]
                if lower:
                    return lower[0]
        return self.qualities[0]


def _height(name: str) -> int:
    match = re.match(r"(\d{3,4})p", name or "")
    return int(match.group(1)) if match else 0


def info_from_ytdlp(info: dict[str, Any], video_id: str) -> VodInfo:
    """Facts and qualities from the dict yt-dlp returns for a ``twitch:vod`` URL."""
    qualities: list[VodQuality] = []
    seen: set[str] = set()
    for fmt in info.get("formats") or ():
        if not isinstance(fmt, dict) or not fmt.get("url"):
            continue
        protocol = str(fmt.get("protocol") or "")
        url = str(fmt["url"])
        if "m3u8" not in protocol and ".m3u8" not in url:
            continue
        format_id = str(fmt.get("format_id") or "")
        audio = fmt.get("vcodec") == "none" or "audio" in format_id.lower()
        height = 0 if audio else int(fmt.get("height") or _height(format_id))
        fps = fmt.get("fps")
        if audio:
            name = "Audio only"
        elif height:
            name = f"{height}p{int(round(fps))}" if fps else f"{height}p"
        else:
            name = format_id or "video"
        if name in seen:
            continue
        seen.add(name)
        source = "source" in format_id.lower() or "chunked" in format_id.lower() or \
            "source" in str(fmt.get("format_note") or "").lower()
        qualities.append(VodQuality(name, url, height, float(fmt.get("tbr") or 0.0), source))
    # Best first; audio last.
    qualities.sort(key=lambda q: (q.height == 0, -q.height, -q.kbps))
    if qualities and not any(q.source for q in qualities) and qualities[0].height:
        best = qualities[0]
        qualities[0] = VodQuality(best.name, best.url, best.height, best.kbps, True)
    created = ""
    timestamp = info.get("timestamp")
    if isinstance(timestamp, (int, float)):
        created = datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
    login = str(info.get("uploader_id") or "").lower()
    channel = str(info.get("uploader") or info.get("channel") or login)
    if not login and re.fullmatch(r"[A-Za-z0-9_]{2,40}", channel):
        login = channel.lower()
    return VodInfo(video_id, str(info.get("title") or "").strip(), channel, login,
                   float(info.get("duration") or 0.0), created, tuple(qualities))


def fetch_vod_info(ref: VodRef, ydl_factory: Callable[[dict], Any] | None = None) -> VodInfo:
    """Title, channel, length and qualities of a recording, without downloading it."""
    factory = ydl_factory or _ytdlp().YoutubeDL
    try:
        with factory({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(ref.url, download=False)
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises many kinds
        text = str(exc).lower()
        if "subscriber" in text or "sub-only" in text or "403" in text:
            raise TwitchError("This stream recording is for subscribers only (or hidden). "
                              "It can't be downloaded without an account.") from exc
        raise TwitchError(_friendly(exc).replace("This clip", "This stream recording")) from exc
    if not isinstance(info, dict):
        raise TwitchError("Twitch returned no information for this stream recording.")
    vod = info_from_ytdlp(info, ref.video_id)
    if not vod.qualities:
        raise TwitchError("Twitch did not give any video for this recording. "
                          "It may be still processing, for subscribers only or deleted.")
    return vod


# ------------------------------------------------------------------ playlist
@dataclass(frozen=True)
class Segment:
    url: str
    start_s: float  # from the start of the recording
    duration_s: float


@dataclass(frozen=True)
class Playlist:
    segments: tuple[Segment, ...]
    init_url: str = ""  # #EXT-X-MAP (fragmented MP4 recordings)

    @property
    def duration_s(self) -> float:
        return self.segments[-1].start_s + self.segments[-1].duration_s if self.segments else 0.0


def parse_playlist(text: str, base_url: str) -> Playlist:
    """The pieces of a media playlist with their times."""
    segments: list[Segment] = []
    init_url = ""
    t = 0.0
    duration: float | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            try:
                duration = float(line[8:].split(",", 1)[0])
            except ValueError:
                duration = 0.0
        elif line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                init_url = urllib.parse.urljoin(base_url, match.group(1))
        elif not line.startswith("#"):
            if duration is None:
                continue  # a variant playlist line, not a piece
            segments.append(Segment(urllib.parse.urljoin(base_url, line), t, duration))
            t += duration
            duration = None
    if not segments and "#EXT-X-STREAM-INF" in text:
        raise TwitchError("Twitch gave a list of qualities instead of the video pieces.")
    return Playlist(tuple(segments), init_url)


def pick_segments(playlist: Playlist, start_s: float, end_s: float | None) -> list[Segment]:
    """The pieces that cover ``start_s``–``end_s`` (None = to the end)."""
    end = playlist.duration_s if end_s is None or end_s <= 0 else min(end_s, playlist.duration_s)
    start = max(0.0, start_s)
    return [s for s in playlist.segments if s.start_s + s.duration_s > start + 0.01 and s.start_s < end - 0.01]


# ------------------------------------------------------------------ download
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def file_name(info: VodInfo, start_s: float, end_s: float, ext: str = "mp4") -> str:
    """``Channel - Title [v123] 1-20-00–2-00-00.mp4`` with characters Windows forbids removed."""
    parts = [p for p in (info.channel, info.title) if p]
    stem = _BAD_CHARS.sub(" ", " - ".join(parts) or f"Twitch {info.video_id}")
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:100].rstrip(" .")
    span = f"{format_time(start_s).replace(':', '-')}–{format_time(end_s).replace(':', '-')}"
    return f"{stem} [v{info.video_id}] {span}.{ext}"


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - Twitch CDN
        return response.read()


def _muted_alternatives(url: str) -> list[str]:
    """Twitch mutes pieces with copyrighted music: the playlist may still say
    ``-unmuted.ts`` while only ``-muted.ts`` exists (or the other way round)."""
    if "-unmuted." in url:
        return [url, url.replace("-unmuted.", "-muted.")]
    if "-muted." in url:
        return [url, url.replace("-muted.", "-unmuted.")]
    return [url]


class _Either:
    """Set when the user cancels or when this download stops by itself."""

    def __init__(self, cancel) -> None:
        self._cancel = cancel
        self._own = threading.Event()

    def set(self) -> None:
        self._own.set()

    def is_set(self) -> bool:
        return self._own.is_set() or (self._cancel is not None and self._cancel.is_set())


@dataclass
class VodDownload:
    path: Path
    info: VodInfo
    quality: str
    start_s: float  # exact place in the stream where the file starts
    end_s: float
    muted: int = 0  # pieces Twitch muted (copyrighted music)
    notes: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def _get_piece(get: Callable[[str], bytes], url: str, cancel) -> bytes:
    error: Exception | None = None
    for attempt in range(RETRIES):
        for candidate in _muted_alternatives(url):
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
            try:
                return get(candidate)
            except urllib.error.HTTPError as exc:
                error = exc
                if exc.code in (403, 404):
                    continue  # try the other muted/unmuted name
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                error = exc
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        time.sleep(min(8.0, 1.0 * 2 ** attempt))
    raise TwitchError(f"Could not download a piece of the recording: {error}")


def _remux(ffmpeg: str, joined: Path, target: Path, cancel) -> None:
    tmp = target.with_name(target.stem + ".tmp" + target.suffix)
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+genpts", "-i", str(joined),
               "-map", "0:v?", "-map", "0:a?", "-c", "copy", "-avoid_negative_ts", "make_zero",
               "-movflags", "+faststart", str(tmp)]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags)
    while process.poll() is None:
        if cancel is not None and cancel.is_set():
            process.kill()
            process.wait()
            tmp.unlink(missing_ok=True)
            raise DownloadCancelled()
        time.sleep(0.1)
    error = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    if process.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise TwitchError(f"FFmpeg could not join the pieces: {error.strip()[-400:]}")
    tmp.replace(target)


def download_vod(
    info: VodInfo,
    start_s: float,
    end_s: float | None,
    folder: Path,
    ffmpeg: str,
    *,
    quality: str = "",
    progress: Callable[[float, str], None] | None = None,
    cancel=None,
    get: Callable[[str], bytes] | None = None,
    workers: int = WORKERS,
) -> VodDownload:
    """Download ``start_s``–``end_s`` (None = to the end) of a recording into ``folder``."""
    get = get or _http_get
    chosen = info.quality(quality)
    if chosen is None:
        raise TwitchError("This recording has no video to download.")
    if progress:
        progress(0.0, "Reading the list of pieces…")
    try:
        text = get(chosen.url).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TwitchError(f"Could not reach Twitch. Check your internet connection.\n\n{exc}") from exc
    playlist = parse_playlist(text, chosen.url)
    pieces = pick_segments(playlist, start_s, end_s)
    if not pieces:
        raise TwitchError(f"The recording is only {format_time(playlist.duration_s)} long; "
                          "nothing to download in this range.")
    real_start = pieces[0].start_s
    real_end = pieces[-1].start_s + pieces[-1].duration_s
    folder.mkdir(parents=True, exist_ok=True)
    ext = "m4a" if chosen.height == 0 else "mp4"
    target = folder / file_name(info, real_start, real_end, ext)
    result = VodDownload(target, info, chosen.name, real_start, real_end,
                         sum(1 for p in pieces if "-muted." in p.url))
    if target.is_file() and target.stat().st_size > 0:
        if progress:
            progress(1.0, "Already downloaded")
        return result

    parts = folder / f".parts v{info.video_id} {int(real_start)}-{int(real_end)} {chosen.name}"
    parts.mkdir(parents=True, exist_ok=True)
    piece_ext = ".mp4" if playlist.init_url else ".ts"
    files = [parts / f"{i:05d}{piece_ext}" for i in range(len(pieces))]
    lock = threading.Lock()
    state = {"done": sum(1 for f in files if f.is_file()), "fresh": 0, "bytes": 0, "t0": time.monotonic()}
    stop = _Either(cancel)  # the user's Cancel, or a piece that failed for good
    total = len(pieces)

    def report() -> None:
        if progress is None:
            return
        done = state["done"]
        elapsed = max(0.001, time.monotonic() - state["t0"])
        speed = state["bytes"] / elapsed / 1_048_576
        text = f"Downloading… {done} of {total} pieces"
        fresh = state["fresh"]
        if speed > 0.05:
            text += f" • {speed:.1f} MB/s"
        if fresh >= 3:
            eta = (total - done) * elapsed / fresh
            if 0 < eta < 36_000:
                text += f" • {format_time(eta)} left"
        progress(min(0.95, 0.95 * done / total), text)

    def fetch(index: int) -> None:
        target_piece = files[index]
        if target_piece.is_file():
            return
        data = _get_piece(get, pieces[index].url, stop)
        tmp = target_piece.with_suffix(target_piece.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target_piece)
        with lock:
            state["done"] += 1
            state["fresh"] += 1
            state["bytes"] += len(data)
            report()

    report()
    init = b""
    if playlist.init_url:
        init = _get_piece(get, playlist.init_url, cancel)
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = [pool.submit(fetch, i) for i in range(total)]
        pending = set(futures)
        while pending:
            finished, pending = wait(pending, timeout=0.2, return_when=FIRST_EXCEPTION)
            for future in finished:
                if future.exception() is not None:
                    stop.set()  # stop the other pieces
                    exc = future.exception()
                    if isinstance(exc, DownloadCancelled) and not (cancel is not None and cancel.is_set()):
                        continue  # stopped because another piece failed: report that one
                    raise exc
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    if progress:
        progress(0.96, "Joining the pieces…")
    joined = parts / f"joined{piece_ext}"
    with open(joined, "wb") as out:
        if init:
            out.write(init)
        for piece in files:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
            with open(piece, "rb") as src:
                shutil.copyfileobj(src, out, 4 * 1_048_576)
    _remux(ffmpeg, joined, target, cancel)
    shutil.rmtree(parts, ignore_errors=True)
    if result.muted:
        result.notes.append(f"Twitch muted {result.muted} piece(s) of this range (copyrighted music): "
                            "the sound is silent there.")
    if progress:
        progress(1.0, "Downloaded")
    return result


# ------------------------------------------------------------------ chat
def download_chat(result: VodDownload, cache_dir: Path, *, progress=None, cancel=None, post=None) -> int:
    """Fetch the chat replay of the downloaded range and keep it for the file
    (the Highlights tab finds it there). Returns the number of messages."""
    from core.highlights import chat as chat_mod  # noqa: PLC0415 - numpy-heavy, only when needed

    link = chat_mod.VodLink(result.info.video_id, int(round(result.start_s)))
    messages = chat_mod.fetch_vod_chat(link, result.duration_s, progress=progress, cancel=cancel, post=post)
    source = f"twitch.tv/videos/{result.info.video_id} from {format_time(result.start_s)}"
    chat_mod.save_chat(messages, result.path, cache_dir, source)
    return len(messages)

"""Download a Twitch clip with yt-dlp (no Twitch account or keys needed).

yt-dlp is imported lazily so that the editor still starts when it is not
installed; Twitch changes its site now and then, so the error tells the
user to update it (``pip install -U yt-dlp``)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from core.twitch.clip import ClipLink, TwitchClip, file_name

log = logging.getLogger(__name__)


class TwitchError(RuntimeError):
    pass


class DownloadCancelled(Exception):
    pass


def _ytdlp():
    try:
        import yt_dlp  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise TwitchError("yt-dlp is not installed. Run run.bat again or: pip install yt-dlp") from exc
    return yt_dlp


def fetch_info(link: ClipLink, ydl_factory: Callable[[dict], Any] | None = None) -> TwitchClip:
    """Clip title, channel, game and date without downloading the video."""
    factory = ydl_factory or _ytdlp().YoutubeDL
    try:
        with factory({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(link.url, download=False)
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises many kinds
        raise TwitchError(_friendly(exc)) from exc
    if not isinstance(info, dict):
        raise TwitchError("Twitch returned no information for this clip.")
    return TwitchClip.from_ytdlp(info, link)


def download_clip(
    link: ClipLink,
    folder: Path,
    *,
    progress: Callable[[float, str], None] | None = None,
    cancel=None,
    ydl_factory: Callable[[dict], Any] | None = None,
) -> tuple[Path, TwitchClip]:
    """Download the best quality of a clip into ``folder``.

    Returns the file and the clip facts. An existing file with the same
    name (same clip) is reused instead of downloading it again."""
    factory = ydl_factory or _ytdlp().YoutubeDL
    if progress:
        progress(0.0, "Reading clip information…")
    clip = fetch_info(link, factory)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / file_name(clip)
    if target.is_file() and target.stat().st_size > 0:
        if progress:
            progress(1.0, "Already downloaded")
        return target, clip

    def hook(state: dict) -> None:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        if progress is None:
            return
        if state.get("status") == "downloading":
            total = state.get("total_bytes") or state.get("total_bytes_estimate") or 0
            done = state.get("downloaded_bytes") or 0
            fraction = done / total if total else 0.0
            progress(min(0.99, fraction), f"Downloading… {done / 1_048_576:.1f} MB")
        elif state.get("status") == "finished":
            progress(0.99, "Finishing…")

    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "format": "best[ext=mp4]/best",
        "outtmpl": {"default": str(target.with_suffix(".%(ext)s"))},
        "progress_hooks": [hook],
        "overwrites": True,
        "noplaylist": True,
    }
    try:
        with factory(options) as ydl:
            info = ydl.extract_info(link.url, download=True)
    except DownloadCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled() from exc
        raise TwitchError(_friendly(exc)) from exc
    path = _downloaded_path(info, target)
    if path is None:
        raise TwitchError("The download finished but the file was not found.")
    if progress:
        progress(1.0, "Downloaded")
    return path, clip


def _downloaded_path(info: Any, target: Path) -> Path | None:
    candidates = []
    if isinstance(info, dict):
        for item in info.get("requested_downloads") or ():
            if isinstance(item, dict) and item.get("filepath"):
                candidates.append(Path(item["filepath"]))
        if info.get("filepath"):
            candidates.append(Path(info["filepath"]))
    candidates.append(target)
    candidates.extend(sorted(target.parent.glob(target.stem + ".*")))
    return next((p for p in candidates if p.is_file() and p.suffix not in (".part", ".ytdl")), None)


def _friendly(exc: Exception) -> str:
    text = str(exc).replace("ERROR: ", "").strip()
    lower = text.lower()
    if "unable to download" in lower or "urlopen" in lower or "getaddrinfo" in lower:
        return f"Could not reach Twitch. Check your internet connection.\n\n{text}"
    if "does not exist" in lower or "404" in lower or "not found" in lower:
        return "This clip was not found. It may have been deleted."
    return f"{text}\n\nIf Twitch changed something, updating yt-dlp usually helps: pip install -U yt-dlp"

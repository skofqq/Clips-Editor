"""Twitch clip links and the facts the editor needs about a clip."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_SLUG = r"[A-Za-z0-9_-]{4,120}"
_PATTERNS = (
    # https://clips.twitch.tv/SlugName or /embed?clip=SlugName
    re.compile(rf"^(?:https?://)?clips\.twitch\.tv/(?:embed\?(?:.*&)?clip=)?(?P<slug>{_SLUG})(?:[/?#&].*)?$", re.I),
    # https://www.twitch.tv/channel/clip/SlugName
    re.compile(rf"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/(?P<login>[A-Za-z0-9_]{{2,40}})/clip/(?P<slug>{_SLUG})"
               r"(?:[/?#].*)?$", re.I),
)


@dataclass(frozen=True)
class ClipLink:
    slug: str
    login: str = ""  # channel login when the link names it

    @property
    def url(self) -> str:
        if self.login:
            return f"https://www.twitch.tv/{self.login}/clip/{self.slug}"
        return f"https://clips.twitch.tv/{self.slug}"


def parse_clip_url(text: str) -> ClipLink | None:
    """A Twitch clip link (any of the usual forms), or None."""
    text = (text or "").strip().strip("<>\"'")
    for pattern in _PATTERNS:
        match = pattern.match(text)
        if match:
            groups = match.groupdict()
            if groups.get("login", "").lower() in ("videos", "directory"):
                return None
            return ClipLink(slug=groups["slug"], login=(groups.get("login") or "").lower())
    return None


@dataclass(frozen=True)
class TwitchClip:
    slug: str
    url: str
    title: str = ""
    channel: str = ""  # broadcaster display name
    login: str = ""  # broadcaster login (twitch.tv/<login>)
    game: str = ""
    clipper: str = ""
    created: str = ""  # ISO date (YYYY-MM-DD) of the clip
    duration_s: float = 0.0
    view_count: int = 0

    @property
    def channel_url(self) -> str:
        return f"https://www.twitch.tv/{self.login}" if self.login else ""

    @classmethod
    def from_ytdlp(cls, info: dict[str, Any], link: ClipLink) -> "TwitchClip":
        """Build from the info dict yt-dlp returns for a twitch:clips URL."""
        channel = str(info.get("channel") or info.get("creator") or "")
        creators = info.get("creators")
        if not channel and isinstance(creators, (list, tuple)) and creators:
            channel = str(creators[0])
        login = link.login
        if not login and re.fullmatch(r"[A-Za-z0-9_]{2,40}", channel):
            login = channel.lower()  # display names of latin channels equal the login
        categories = info.get("categories") or ()
        game = str(categories[0]) if isinstance(categories, (list, tuple)) and categories else ""
        created = ""
        timestamp = info.get("timestamp")
        if isinstance(timestamp, (int, float)):
            created = datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
        elif isinstance(info.get("upload_date"), str) and len(info["upload_date"]) == 8:
            d = info["upload_date"]
            created = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        return cls(
            slug=link.slug,
            url=link.url,
            title=str(info.get("title") or "").strip(),
            channel=channel,
            login=login,
            game=game,
            clipper=str(info.get("uploader") or ""),
            created=created,
            duration_s=float(info.get("duration") or 0.0),
            view_count=int(info.get("view_count") or 0),
        )


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def file_name(clip: TwitchClip, ext: str = "mp4") -> str:
    """``Channel - Title [slug].mp4`` with characters Windows forbids removed."""
    parts = [p for p in (clip.channel, clip.title) if p]
    stem = " - ".join(parts) or clip.slug
    stem = _BAD_CHARS.sub(" ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:120].rstrip(" .")
    return f"{stem} [{clip.slug}].{ext}"

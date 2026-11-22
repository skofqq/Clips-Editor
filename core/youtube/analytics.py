"""Shorts analytics: how the channel's videos do.

Views, likes and comments come from the YouTube Data API (the permission the
app already has). Watch time, the average share watched and subscribers
gained come from the YouTube Analytics API; that needs the Analytics
permission (reconnect older accounts) and the "YouTube Analytics API"
enabled in the Google Cloud project. Without it the basic numbers still show.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass, replace
from datetime import date, timedelta

from core.youtube import api as api_mod
from core.youtube.errors import YouTubeError

log = logging.getLogger(__name__)

ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
SHORTS_MAX_S = 180
_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


@dataclass(frozen=True)
class VideoStats:
    video_id: str
    title: str
    published_at: str  # ISO date-time from YouTube
    duration_s: int
    views: int
    likes: int
    comments: int
    privacy: str = ""
    avg_view_pct: float | None = None  # share of the video watched on average, %
    avg_view_s: float | None = None
    subscribers: int | None = None  # gained from this video
    from_app: bool = False  # uploaded with this app
    tags: tuple[str, ...] = ()  # YouTube tags (the owner sees them)
    hashtags: tuple[str, ...] = ()  # #words in the title and description, without "#"

    @property
    def is_short(self) -> bool:
        return 0 < self.duration_s <= SHORTS_MAX_S

    @property
    def url(self) -> str:
        return f"https://youtube.com/shorts/{self.video_id}"

    @property
    def like_rate(self) -> float:
        """Likes per 100 views."""
        return 100.0 * self.likes / self.views if self.views else 0.0


@dataclass(frozen=True)
class Summary:
    videos: int
    views: int
    likes: int
    comments: int
    subscribers: int | None
    avg_view_pct: float | None  # weighted by views
    best: VideoStats | None


def parse_duration(text: str) -> int:
    """ISO 8601 duration (PT1M5S) in seconds."""
    match = _DURATION.fullmatch(text or "")
    if not match:
        return 0
    d, h, m, s = (int(g or 0) for g in match.groups())
    return ((d * 24 + h) * 60 + m) * 60 + s


_HASHTAG = re.compile(r"(?<![\w&])#(\w+)", re.UNICODE)


def find_hashtags(text: str) -> tuple[str, ...]:
    """#words in ``text`` (without "#"), each once, in order."""
    seen: dict[str, str] = {}
    for word in _HASHTAG.findall(text or ""):
        if not word.isdigit():
            seen.setdefault(word.casefold(), word)
    return tuple(seen.values())


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def channel_video_ids(client, limit: int = 50) -> list[str]:
    """The newest uploads of the client's channel (its "uploads" playlist)."""
    payload = client.get_json(f"{api_mod.API_URL}/channels?part=contentDetails&mine=true")
    items = payload.get("items") or []
    if not items:
        return []
    playlist = ((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads", "")
    if not playlist:
        return []
    query = urllib.parse.urlencode({"part": "contentDetails", "playlistId": playlist,
                                    "maxResults": max(1, min(50, limit))})
    payload = client.get_json(f"{api_mod.API_URL}/playlistItems?{query}")
    return [str((i.get("contentDetails") or {}).get("videoId", "")) for i in payload.get("items") or []
            if (i.get("contentDetails") or {}).get("videoId")]


def video_stats(client, video_ids: list[str]) -> list[VideoStats]:
    """Title, length and public counters of ``video_ids`` (50 per request)."""
    result: list[VideoStats] = []
    ids = list(dict.fromkeys(v for v in video_ids if v))
    for start in range(0, len(ids), 50):
        query = urllib.parse.urlencode({"part": "snippet,statistics,contentDetails,status",
                                        "id": ",".join(ids[start:start + 50]), "maxResults": 50})
        payload = client.get_json(f"{api_mod.API_URL}/videos?{query}")
        for item in payload.get("items") or []:
            snippet, stats = item.get("snippet") or {}, item.get("statistics") or {}
            result.append(VideoStats(
                video_id=str(item.get("id", "")),
                title=str(snippet.get("title", "")),
                published_at=str(snippet.get("publishedAt", "")),
                duration_s=parse_duration(str((item.get("contentDetails") or {}).get("duration", ""))),
                views=_int(stats.get("viewCount")),
                likes=_int(stats.get("likeCount")),
                comments=_int(stats.get("commentCount")),
                privacy=str((item.get("status") or {}).get("privacyStatus", "")),
                tags=tuple(str(t) for t in snippet.get("tags") or () if str(t).strip()),
                hashtags=find_hashtags(str(snippet.get("title", "")) + "\n" + str(snippet.get("description", ""))),
            ))
    return result


def retention(client, video_ids: list[str], start: date, end: date) -> dict[str, tuple[float, float, int]]:
    """{video_id: (average % watched, average seconds watched, subscribers gained)}."""
    found: dict[str, tuple[float, float, int]] = {}
    ids = list(dict.fromkeys(v for v in video_ids if v))
    for first in range(0, len(ids), 200):
        query = urllib.parse.urlencode({
            "ids": "channel==MINE",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "metrics": "averageViewPercentage,averageViewDuration,subscribersGained",
            "dimensions": "video",
            "filters": "video==" + ",".join(ids[first:first + 200]),
            "maxResults": 200,
        })
        payload = client.get_json(f"{ANALYTICS_URL}?{query}")
        names = [str(h.get("name", "")) for h in payload.get("columnHeaders") or []]
        for row in payload.get("rows") or []:
            values = dict(zip(names, row))
            vid = str(values.get("video", ""))
            if vid:
                found[vid] = (float(values.get("averageViewPercentage") or 0.0),
                              float(values.get("averageViewDuration") or 0.0),
                              _int(values.get("subscribersGained")))
    return found


def collect(client, app_video_ids: list[str], limit: int = 50, today: date | None = None,
            progress=None, cancel=None) -> tuple[list[VideoStats], str]:
    """Stats of the channel's newest uploads plus every upload made with the app.

    Returns (stats newest first, note); the note says why retention is missing."""
    if progress is not None:
        progress(0.1, "Reading the channel's videos…")
    ids = channel_video_ids(client, limit)
    app_ids = set(app_video_ids)
    ids += [v for v in app_video_ids if v not in ids]
    if progress is not None:
        progress(0.4, "Reading views and likes…")
    stats = [replace(s, from_app=s.video_id in app_ids) for s in video_stats(client, ids)]
    note = ""
    if stats:
        if progress is not None:
            progress(0.7, "Reading watch time…")
        end = today or date.today()
        oldest = min((s.published_at[:10] for s in stats if s.published_at), default=end.isoformat())
        try:
            start = date.fromisoformat(oldest)
        except ValueError:
            start = end - timedelta(days=365)
        try:
            extra = retention(client, [s.video_id for s in stats], min(start, end), end)
        except YouTubeError as exc:
            log.info("No analytics: %s", exc)
            note = analytics_note(exc)
        else:
            stats = [replace(s, avg_view_pct=extra[s.video_id][0], avg_view_s=extra[s.video_id][1],
                             subscribers=extra[s.video_id][2]) if s.video_id in extra else s for s in stats]
    stats.sort(key=lambda s: s.published_at, reverse=True)
    return stats, note


def analytics_note(exc: YouTubeError) -> str:
    text = str(exc).lower()
    if "accessnotconfigured" in text or "has not been used" in text or "is disabled" in text \
            or getattr(exc, "reason", "") == "accessNotConfigured":
        return ("Watch time is not available: enable “YouTube Analytics API” in your Google Cloud project "
                "(APIs & Services → Library), then refresh.")
    if getattr(exc, "status", 0) == 403 or getattr(exc, "status", 0) == 401:
        return ("Watch time is not available: reconnect the channel (Accounts…) and allow viewing "
                "YouTube Analytics reports.")
    return f"Watch time is not available: {exc}"


def summarize(stats: list[VideoStats]) -> Summary:
    views = sum(s.views for s in stats)
    with_pct = [s for s in stats if s.avg_view_pct is not None and s.views]
    weight = sum(s.views for s in with_pct)
    pct = sum(s.avg_view_pct * s.views for s in with_pct) / weight if weight else None
    subs = [s.subscribers for s in stats if s.subscribers is not None]
    return Summary(
        videos=len(stats),
        views=views,
        likes=sum(s.likes for s in stats),
        comments=sum(s.comments for s in stats),
        subscribers=sum(subs) if subs else None,
        avg_view_pct=pct,
        best=max(stats, key=lambda s: s.views) if stats else None,
    )

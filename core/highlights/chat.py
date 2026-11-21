"""Twitch chat as evidence for the best moments.

When something happens on stream the chat explodes a few seconds later:
many messages at once, laughing emotes (KEKW, LUL, «ахаха») or hype
(Pog, «вау»). This module reads that chat and turns its bursts into
:class:`~core.highlights.finder.Event` objects.

Chat comes from:

* a file saved by a chat downloader (TwitchDownloader JSON or TXT,
  chat-downloader JSON, yt-dlp ``rechat`` JSON, or a plain
  ``[0:01:23] nick: text`` log), or
* the stream recording on Twitch itself (``twitch.tv/videos/<id>``),
  downloaded through Twitch's public web API without an account.

Times are seconds from the start of the **local file** (an offset is
applied when the file starts later in the stream).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from core.highlights.finder import CHAT, CHAT_HYPE, CHAT_LAUGH, HIGH, LOW, NORMAL, Event

log = logging.getLogger(__name__)

CHAT_DELAY_S = 6.0  # stream delay + reading + typing: chat reacts this much after the moment
RATE_WINDOW_S = 5  # messages are counted over this many seconds
BASELINE_S = 121  # compared with the usual pace over about two minutes
MIN_MESSAGES = 3  # a "burst" needs at least this many messages in RATE_WINDOW_S
MIN_GAP_S = 8  # chat bursts closer than this are one event
RATIO = {LOW: 3.0, NORMAL: 2.2, HIGH: 1.7}  # how many times faster than usual

_LAUGH = re.compile(
    r"(?:\b(?:KEKW|KEKL|KEK|LUL|LULW|OMEGALUL|ICANT|pepeLaugh|LMAO|LOL|xd|4Head|ROFL|kekw|lul|lmao|lol)\b|"
    r"(?:a?ха){2,}|(?:ah){2,}|(?:ха){2,}|\bлол\b|\bкек|\bору|\bржу|\){3,})",
    re.IGNORECASE,
)
_HYPE = re.compile(
    r"(?:\b(?:Pog|PogU|POGGERS|PogChamp|POGGIES|PagMan|PagChomp|WOW|EZ|Clap|GIGACHAD|monkaS|monkaW|"
    r"WAYTOODANK|OMG|WTF|insane|clutch|LETSGO|LETS GO|wow|pog|вау|ого+|имба|жесть|клатч|красава|"
    r"нифига|офигеть|пиздец|ебать)\b|!{3,}|\?{3,})",
    re.IGNORECASE,
)


class ChatError(RuntimeError):
    pass


class ChatCancelled(Exception):
    pass


@dataclass(frozen=True)
class ChatMessage:
    t: float  # seconds from the start of the local file
    text: str
    author: str = ""


def message_kind(text: str) -> str:
    """CHAT_LAUGH, CHAT_HYPE or "" for one chat message."""
    if _LAUGH.search(text):
        return CHAT_LAUGH
    if _HYPE.search(text):
        return CHAT_HYPE
    return ""


# ------------------------------------------------------------------ files
_TIME = r"(?:(?P<h>\d{1,2}):)?(?P<m>\d{1,2}):(?P<s>\d{2})(?:[.,]\d+)?"
_TXT_LINE = re.compile(rf"^\s*\[?\s*{_TIME}\s*\]?\s+(?:<(?P<a1>[^>]{{1,40}})>|(?P<a2>[^:\s][^:]{{0,39}}):)\s*(?P<text>.*)$")


def _seconds(match: re.Match) -> float:
    return int(match["h"] or 0) * 3600 + int(match["m"]) * 60 + int(match["s"])


def _from_comment(item: Any) -> ChatMessage | None:
    """One message in any of the JSON layouts chat downloaders use."""
    if not isinstance(item, dict):
        return None
    t = item.get("content_offset_seconds", item.get("contentOffsetSeconds", item.get("time_in_seconds")))
    if t is None and isinstance(item.get("time_in_ms"), (int, float)):
        t = item["time_in_ms"] / 1000.0
    if not isinstance(t, (int, float)):
        return None
    message = item.get("message")
    if isinstance(message, dict):
        text = message.get("body")
        if text is None:
            text = "".join(str(f.get("text", "")) for f in message.get("fragments") or () if isinstance(f, dict))
    else:
        text = message if isinstance(message, str) else item.get("body", "")
    author = item.get("commenter") or item.get("author") or {}
    if isinstance(author, dict):
        author = author.get("display_name") or author.get("displayName") or author.get("name") or ""
    return ChatMessage(float(t), str(text or ""), str(author or ""))


def parse_chat(text: str) -> list[ChatMessage]:
    """Messages from the contents of a chat file (JSON or text)."""
    text = text.lstrip("﻿")
    stripped = text.lstrip()
    messages: list[ChatMessage] = []
    if stripped[:1] in "[{":
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if data is not None:
            items = data.get("comments") if isinstance(data, dict) else data
            if isinstance(data, dict) and items is None:
                items = data.get("messages") or ()
            for item in items or ():
                message = _from_comment(item)
                if message is not None:
                    messages.append(message)
            return sorted(messages, key=lambda m: m.t)
        # JSON lines (one message per line), e.g. chat-downloader's output
        for line in stripped.splitlines():
            try:
                message = _from_comment(json.loads(line))
            except ValueError:
                continue
            if message is not None:
                messages.append(message)
        if messages:
            return sorted(messages, key=lambda m: m.t)
    for line in text.splitlines():
        match = _TXT_LINE.match(line)
        if match:
            author = (match["a1"] or match["a2"] or "").strip()
            messages.append(ChatMessage(float(_seconds(match)), match["text"].strip(), author))
    return sorted(messages, key=lambda m: m.t)


def read_chat_file(path: str | Path, offset_s: float = 0.0) -> list[ChatMessage]:
    """Read a chat file; ``offset_s`` = where the local file starts in the stream."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ChatError(f"Could not read {path.name}: {exc}") from exc
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", errors="replace")
    messages = parse_chat(text)
    if offset_s == 0.0:
        offset_s = file_offset(text)
    if not messages:
        raise ChatError(
            f"No chat messages with times were found in {path.name}.\n\n"
            "Use a JSON or TXT file from TwitchDownloader (relative timestamps) or a log with "
            "lines like “[0:01:23] nick: message”."
        )
    return shift(messages, offset_s)


def file_offset(text: str) -> float:
    """Where a trimmed chat download starts in the stream (TwitchDownloader
    writes it as ``video.start``); times in its comments count from the
    stream start, the local video from that point."""
    stripped = text.lstrip("\ufeff").lstrip()
    if not stripped.startswith("{"):
        return 0.0
    try:
        data = json.loads(stripped)
    except ValueError:
        return 0.0
    video = data.get("video") if isinstance(data, dict) else None
    start = video.get("start") if isinstance(video, dict) else None
    return float(start) if isinstance(start, (int, float)) and start > 0 else 0.0


def shift(messages: Iterable[ChatMessage], offset_s: float, duration_s: float | None = None) -> list[ChatMessage]:
    """Times relative to a file that starts ``offset_s`` into the stream."""
    out = []
    for m in messages:
        t = m.t - offset_s
        if t < 0 or (duration_s is not None and t > duration_s):
            continue
        out.append(ChatMessage(t, m.text, m.author))
    return out


def cut(messages: Iterable[ChatMessage], start_s: float, end_s: float) -> list[ChatMessage]:
    """The chat of a part of the file, with times from the start of that part."""
    return shift((m for m in messages if start_s <= m.t <= end_s), start_s)


# ------------------------------------------------------------------ cache
def cache_file(media: str | Path, cache_dir: Path) -> Path:
    key = str(media).replace("\\", "/").lower()
    return Path(cache_dir) / "chat" / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()[:20]}.json.gz"


def save_chat(messages: list[ChatMessage], media: str | Path, cache_dir: Path, source: str = "") -> Path:
    target = cache_file(media, cache_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": 1, "source": source, "messages": [[round(m.t, 2), m.text, m.author] for m in messages]}
    tmp = target.with_name(target.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(target)
    return target


def load_chat(media: str | Path, cache_dir: Path) -> tuple[list[ChatMessage], str] | None:
    """(messages, source) saved for this media, or None."""
    target = cache_file(media, cache_dir)
    if not target.is_file():
        return None
    try:
        with gzip.open(target, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        messages = [ChatMessage(float(t), str(text), str(author)) for t, text, author in data["messages"]]
        return messages, str(data.get("source", ""))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("Ignoring unreadable chat cache %s: %s", target, exc)
        return None


def forget_chat(media: str | Path, cache_dir: Path) -> None:
    try:
        cache_file(media, cache_dir).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not remove chat cache: %s", exc)


# ------------------------------------------------------------------ events
def chat_rate(messages: Iterable[ChatMessage], duration_s: float, delay_s: float = CHAT_DELAY_S):
    """(per-second message counts over RATE_WINDOW_S, laugh counts, hype counts),
    moved ``delay_s`` earlier so that they line up with the moment itself."""
    n = max(1, int(np.ceil(duration_s)) + 1)
    total, laugh, hype = np.zeros(n), np.zeros(n), np.zeros(n)
    for m in messages:
        i = int(m.t - delay_s)
        if 0 <= i < n:
            total[i] += 1
            kind = message_kind(m.text)
            if kind == CHAT_LAUGH:
                laugh[i] += 1
            elif kind == CHAT_HYPE:
                hype[i] += 1
    box = np.ones(RATE_WINDOW_S)
    return (np.convolve(total, box, "same"), np.convolve(laugh, box, "same"), np.convolve(hype, box, "same"))


def chat_events(messages: list[ChatMessage], duration_us: int, sensitivity: str = NORMAL,
                delay_s: float = CHAT_DELAY_S) -> list[Event]:
    """Moments when the chat suddenly writes much faster than usual."""
    if not messages:
        return []
    duration_s = duration_us / 1_000_000
    rate, laugh, hype = chat_rate(messages, duration_s, delay_s)
    if rate.size < 3:
        return []
    from core.highlights.finder import _rolling_median  # noqa: PLC0415

    baseline = _rolling_median(rate, BASELINE_S) if rate.size > BASELINE_S else np.full(rate.size, np.median(rate))
    ratio = rate / np.maximum(baseline, 1.0)
    threshold = RATIO.get(sensitivity, RATIO[NORMAL])
    candidates = np.where((ratio >= threshold) & (rate >= MIN_MESSAGES))[0]
    taken = np.zeros(rate.size, dtype=bool)
    events: list[Event] = []
    for i in sorted(candidates, key=lambda k: -ratio[k]):
        if taken[i]:
            continue
        taken[max(0, i - MIN_GAP_S + 1):i + MIN_GAP_S] = True
        score = 0.3 + (ratio[i] - threshold) / 5.0
        reason = CHAT
        if laugh[i] >= 0.3 * rate[i]:
            reason, score = CHAT_LAUGH, score + 0.1
        elif hype[i] >= 0.3 * rate[i]:
            reason, score = CHAT_HYPE, score + 0.1
        events.append(Event(int(i * 1_000_000), float(np.clip(score, 0.3, 0.9)), reason))
    return sorted(events, key=lambda e: e.t_us)


def count_in(messages: list[ChatMessage], start_s: float, end_s: float, delay_s: float = CHAT_DELAY_S) -> int:
    """Messages reacting to what happens between start and end."""
    return sum(1 for m in messages if start_s <= m.t - delay_s <= end_s)


# ------------------------------------------------------------------ Twitch
_VOD = re.compile(r"(?:twitch\.tv/(?:[A-Za-z0-9_]+/)?(?:videos?|v)/|^)(?P<id>\d{6,12})(?:\b|$)", re.IGNORECASE)
_T = re.compile(r"[?&#]t=(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s?)?")


@dataclass(frozen=True)
class VodLink:
    video_id: str
    offset_s: int = 0  # where the local file starts in the stream (?t=1h2m3s)


def parse_vod_url(text: str) -> VodLink | None:
    text = (text or "").strip().strip("<>\"'")
    match = _VOD.search(text)
    if not match:
        return None
    offset = 0
    t = _T.search(text)
    if t and any(t.groupdict().values()):
        offset = int(t["h"] or 0) * 3600 + int(t["m"] or 0) * 60 + int(t["s"] or 0)
    return VodLink(match["id"], offset)


GQL_URL = "https://gql.twitch.tv/gql"
# Twitch's public web player API; the same values chat downloaders use. No account needed.
GQL_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


def _gql_post(body: Any, timeout: float = 30.0) -> Any:
    request = urllib.request.Request(
        GQL_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Client-ID": GQL_CLIENT_ID, "Content-Type": "text/plain;charset=UTF-8",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
        return json.loads(response.read().decode("utf-8"))


def fetch_vod_chat(
    link: VodLink,
    duration_s: float,
    *,
    progress: Callable[[float, str], None] | None = None,
    cancel=None,
    post: Callable[[Any], Any] | None = None,
) -> list[ChatMessage]:
    """Download the chat replay of a Twitch stream recording for the part
    the local file covers. Times are relative to the local file."""
    post = post or _gql_post
    start, end = float(link.offset_s), float(link.offset_s) + duration_s
    messages: list[ChatMessage] = []
    variables: dict[str, Any] = {"videoID": link.video_id, "contentOffsetSeconds": int(start)}
    seen: set[str] = set()
    pages = 0
    while True:
        if cancel is not None and cancel.is_set():
            raise ChatCancelled()
        body = [{"operationName": "VideoCommentsByOffsetOrCursor", "variables": variables,
                 "extensions": {"persistedQuery": {"version": 1, "sha256Hash": COMMENTS_HASH}}}]
        data = None
        for attempt in range(3):
            try:
                data = post(body)
                break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if attempt == 2:
                    raise ChatError(f"Could not reach Twitch: {exc}") from exc
                time.sleep(1.5 * (attempt + 1))
        page = data[0] if isinstance(data, list) and data else data
        if not isinstance(page, dict):
            raise ChatError("Twitch sent an unexpected answer.")
        if page.get("errors"):
            message = "; ".join(str(e.get("message", e)) for e in page["errors"] if e)
            raise ChatError(f"Twitch refused the chat request ({message}).\n\n"
                            "Download the chat with TwitchDownloader and open the file instead.")
        video = (page.get("data") or {}).get("video")
        if video is None:
            raise ChatError("This stream recording was not found. It may be deleted or for subscribers only.")
        comments = video.get("comments") or {}
        edges = comments.get("edges") or []
        last_t = start
        for edge in edges:
            node = (edge or {}).get("node") or {}
            node_id = str(node.get("id") or "")
            if node_id and node_id in seen:
                continue
            seen.add(node_id)
            message = _from_comment(node)
            if message is None:
                continue
            last_t = message.t
            if start <= message.t <= end:
                messages.append(ChatMessage(message.t - start, message.text, message.author))
        pages += 1
        if progress is not None and duration_s > 0:
            done = min(1.0, max(0.0, (last_t - start) / duration_s))
            progress(done, f"Chat: {len(messages):,} messages ({done:.0%})".replace(",", " "))
        has_next = (comments.get("pageInfo") or {}).get("hasNextPage")
        cursor = edges[-1].get("cursor") if edges and isinstance(edges[-1], dict) else None
        if not edges or not has_next or not cursor or last_t > end:
            break
        variables = {"videoID": link.video_id, "cursor": cursor}
        if pages % 20 == 0:
            time.sleep(0.2)  # be gentle with Twitch on long streams
    messages.sort(key=lambda m: m.t)
    return messages

"""Fill the Data page from a Twitch clip or stream recording link.

Paste ``https://clips.twitch.tv/…``, ``https://www.twitch.tv/<channel>/clip/…``,
``https://www.twitch.tv/videos/<id>?t=1h2m3s`` or a channel link and the app
asks Twitch (the public web API the chat download uses, no account) who
streamed, which game, the clip's title and when it was. The streamer, game and
people mentioned with ``@`` in the titles are found in the library or added to
it. Everything stays editable; nothing is changed when Twitch can't be reached,
except what the link itself says (the channel name in it).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

from core.highlights.chat import GQL_CLIENT_ID, GQL_URL, parse_vod_url
from core.metadata.generate import ClipMetadata, MetadataSelection, generate
from core.metadata.library import GAME, PARTICIPANT, STREAMER, Library

CLIP, VIDEO, CHANNEL = "clip", "video", "channel"

_SLUG = r"(?P<slug>[A-Za-z0-9][A-Za-z0-9_-]{2,})"
_CLIP_PATTERNS = (
    re.compile(r"clips\.twitch\.tv/(?:embed\?(?:[^#]*&)?clip=)?" + _SLUG, re.IGNORECASE),
    re.compile(r"twitch\.tv/(?P<login>[A-Za-z0-9_]{2,25})/clip/" + _SLUG, re.IGNORECASE),
    re.compile(r"twitch\.tv/clip/" + _SLUG, re.IGNORECASE),
)
_CHANNEL = re.compile(r"twitch\.tv/(?P<login>[A-Za-z0-9_]{2,25})/?(?:[?#].*)?$", re.IGNORECASE)
# twitch.tv/<this> are pages, not channels.
_NOT_CHANNELS = {"videos", "directory", "settings", "subscriptions", "inventory", "drops", "wallet", "downloads",
                 "search", "p", "jobs", "turbo", "friends", "messages", "clip", "embed", "popout", "moderator"}
_MENTION = re.compile(r"(?<![\w@])@(?P<login>[A-Za-z0-9_]{3,25})\b")


class TwitchLinkError(Exception):
    """Not a Twitch link, or Twitch could not be asked."""


@dataclass(frozen=True)
class TwitchLink:
    kind: str  # CLIP, VIDEO or CHANNEL
    id: str  # clip slug, video id or channel login
    login: str = ""  # channel in the link, when it has one
    offset_s: int = 0  # ?t= of a stream recording link


@dataclass(frozen=True)
class TwitchInfo:
    """What a link says about the clip; empty strings where Twitch had nothing."""

    link: TwitchLink
    streamer_login: str = ""
    streamer_name: str = ""
    game: str = ""
    clip_title: str = ""  # only for clips whose title was written for the clip
    stream_title: str = ""
    date: str = ""  # dd.mm.yyyy in local time, like the Date field
    stream_url: str = ""
    mentions: tuple[tuple[str, str], ...] = ()  # (login, display name) of people named with @
    online: bool = True  # False: Twitch was not reached, only the link itself was read
    error: str = ""


def parse_link(text: str) -> TwitchLink | None:
    """The clip, stream recording or channel a pasted text points to."""
    text = (text or "").strip().strip("<>\"'")
    if not text:
        return None
    for pattern in _CLIP_PATTERNS:
        match = pattern.search(text)
        if match:
            slug = match["slug"]
            if slug.lower() in ("embed",):
                continue
            login = match.groupdict().get("login") or ""
            return TwitchLink(CLIP, slug, login.lower())
    if "twitch.tv" in text.lower() or text.isdigit():
        vod = parse_vod_url(text)
        if vod is not None:
            return TwitchLink(VIDEO, vod.video_id, "", vod.offset_s)
    match = _CHANNEL.search(text)
    if match and match["login"].lower() not in _NOT_CHANNELS:
        return TwitchLink(CHANNEL, match["login"].lower(), match["login"].lower())
    return None


# ------------------------------------------------------------------ Twitch
_CLIP_QUERY = """query($slug: ID!) { clip(slug: $slug) {
  title createdAt videoOffsetSeconds
  broadcaster { login displayName }
  game { name displayName }
  video { id title createdAt }
} }"""
_VIDEO_QUERY = """query($id: ID!) { video(id: $id) {
  id title createdAt
  owner { login displayName }
  game { name displayName }
} }"""
_USER_QUERY = """query($login: String!) { user(login: $login) {
  login displayName
  broadcastSettings { title game { name displayName } }
} }"""
_USERS_QUERY = """query($logins: [String!]) { users(logins: $logins) { login displayName } }"""


def _gql_post(body: Any, timeout: float = 20.0) -> Any:
    request = urllib.request.Request(
        GQL_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Client-ID": GQL_CLIENT_ID, "Content-Type": "text/plain;charset=UTF-8",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
        return json.loads(response.read().decode("utf-8"))


def _ask(post: Callable[[Any], Any], query: str, variables: dict) -> dict:
    try:
        answer = post({"query": query, "variables": variables})
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise TwitchLinkError(f"Could not reach Twitch: {exc}") from exc
    page = answer[0] if isinstance(answer, list) and answer else answer
    if not isinstance(page, dict):
        raise TwitchLinkError("Twitch sent an unexpected answer.")
    data = page.get("data")
    if page.get("errors") and not data:
        message = "; ".join(str((e or {}).get("message", e)) for e in page["errors"])
        raise TwitchLinkError(f"Twitch refused the request ({message}).")
    return data or {}


def _local_date(stamp: str) -> str:
    if not stamp:
        return ""
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%d.%m.%Y")


def _hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return (f"{h}h" if h else "") + (f"{m}m" if h or m else "") + f"{s}s"


def video_url(video_id: str, offset_s: int = 0) -> str:
    return f"https://www.twitch.tv/videos/{video_id}" + (f"?t={_hms(offset_s)}" if offset_s > 0 else "")


def channel_url(login: str) -> str:
    return f"https://www.twitch.tv/{login}" if login else ""


def _game(node: Any) -> str:
    node = node or {}
    return str(node.get("displayName") or node.get("name") or "").strip()


def _person(node: Any) -> tuple[str, str]:
    node = node or {}
    login = str(node.get("login") or "").strip().lower()
    return login, str(node.get("displayName") or login).strip()


def mentions_in(*texts: str) -> list[str]:
    """Logins named with ``@`` in the titles, first seen first."""
    seen: dict[str, None] = {}
    for text in texts:
        for match in _MENTION.finditer(text or ""):
            seen.setdefault(match["login"].lower(), None)
    return list(seen)


def _clean_title(title: str, names: dict[str, str]) -> str:
    """Clip title with ``@login`` written as the person's name."""
    title = _MENTION.sub(lambda m: names.get(m["login"].lower(), m["login"]), title or "")
    return re.sub(r"\s+", " ", title).strip()


def offline_info(link: TwitchLink, error: str = "") -> TwitchInfo:
    """What can be read from the link alone."""
    stream_url = video_url(link.id, link.offset_s) if link.kind == VIDEO else channel_url(link.login)
    return TwitchInfo(link, streamer_login=link.login, streamer_name=link.login, stream_url=stream_url,
                      online=False, error=error)


def fetch_info(text: str, post: Callable[[Any], Any] | None = None) -> TwitchInfo:
    """Ask Twitch about a pasted link. Raises TwitchLinkError for something that is not a Twitch link
    or a clip that does not exist; when Twitch can't be reached returns what the link itself says."""
    link = parse_link(text)
    if link is None:
        raise TwitchLinkError("This is not a Twitch clip, stream recording or channel link.")
    post = post or _gql_post
    try:
        info = _fetch(link, post)
    except TwitchLinkError as exc:
        if link.login:
            return offline_info(link, str(exc))
        if link.kind == VIDEO:
            return offline_info(link, str(exc))
        raise
    names: dict[str, str] = {}
    logins = [m for m in mentions_in(info.clip_title, info.stream_title) if m != info.streamer_login]
    if logins:
        try:
            data = _ask(post, _USERS_QUERY, {"logins": logins})
            for node in data.get("users") or []:
                login, name = _person(node)
                if login:
                    names[login] = name
        except TwitchLinkError:
            pass  # the logins themselves are good enough
    return replace(info, clip_title=_clean_title(info.clip_title, names),
                   mentions=tuple((login, names.get(login, login)) for login in logins))


def _fetch(link: TwitchLink, post: Callable[[Any], Any]) -> TwitchInfo:
    if link.kind == CLIP:
        clip = _ask(post, _CLIP_QUERY, {"slug": link.id}).get("clip")
        if not clip:
            raise TwitchLinkError("This clip was not found on Twitch. It may have been deleted.")
        login, name = _person(clip.get("broadcaster"))
        video = clip.get("video") or {}
        title = str(clip.get("title") or "").strip()
        stream_title = str(video.get("title") or "").strip()
        # A clip nobody renamed carries the stream's title: that says nothing about the moment.
        clip_title = "" if stream_title and title.casefold() == stream_title.casefold() else title
        offset = int(clip.get("videoOffsetSeconds") or 0)
        url = video_url(str(video["id"]), offset) if video.get("id") else channel_url(login or link.login)
        return TwitchInfo(link, login or link.login, name or link.login, _game(clip.get("game")), clip_title,
                          stream_title, _local_date(str(video.get("createdAt") or clip.get("createdAt") or "")),
                          url)
    if link.kind == VIDEO:
        video = _ask(post, _VIDEO_QUERY, {"id": link.id}).get("video")
        if not video:
            raise TwitchLinkError("This stream recording was not found. It may be deleted or for subscribers only.")
        login, name = _person(video.get("owner"))
        return TwitchInfo(link, login, name, _game(video.get("game")), "", str(video.get("title") or "").strip(),
                          _local_date(str(video.get("createdAt") or "")), video_url(link.id, link.offset_s))
    user = _ask(post, _USER_QUERY, {"login": link.id}).get("user")
    if not user:
        raise TwitchLinkError(f"There is no Twitch channel “{link.id}”.")
    login, name = _person(user)
    settings = user.get("broadcastSettings") or {}
    return TwitchInfo(link, login, name, _game(settings.get("game")), "", str(settings.get("title") or "").strip(),
                      "", channel_url(login))


# ------------------------------------------------------------------ library
@dataclass(frozen=True)
class Filled:
    metadata: ClipMetadata
    filled: tuple[str, ...] = ()  # which fields, in words ("streamer", "game", …)
    added: tuple[str, ...] = ()  # profiles added to the library
    notes: tuple[str, ...] = field(default_factory=tuple)


def _find_person(library: Library, login: str, name: str):
    for kind in (STREAMER, PARTICIPANT):
        for text in (login, name):
            if text:
                found = library.find(kind, text)
                if found is not None:
                    return found
    return None


def _person_id(library: Library, kind: str, login: str, name: str, added: list[str]) -> str:
    person = _find_person(library, login, name)
    if person is None:
        person = library.create(kind, name or login, username=login, twitch_url=channel_url(login))
        added.append(person.name)
    return person.id


def apply_info(current: ClipMetadata, info: TwitchInfo, library: Library) -> Filled:
    """``current`` with what the link told; title, description and tags made again from it.
    Hand-edited title, description and tags stay as they are."""
    s = current.selection
    filled: list[str] = []
    added: list[str] = []
    notes: list[str] = []
    streamer_id = s.streamer_id
    if info.streamer_login or info.streamer_name:
        streamer_id = _person_id(library, STREAMER, info.streamer_login, info.streamer_name, added)
        filled.append("streamer")
    participants = [p for p in s.participant_ids if p != streamer_id]
    for login, name in info.mentions:
        person_id = _person_id(library, PARTICIPANT, login, name, added)
        if person_id != streamer_id and person_id not in participants:
            participants.append(person_id)
            if "participants" not in filled:
                filled.append("participants")
    game_id = s.game_id
    if info.game:
        game = library.find(GAME, info.game)
        if game is None:
            game = library.create(GAME, info.game)
            added.append(game.name)
        game_id = game.id
        filled.append("game")
    clip_title = s.clip_title
    if info.clip_title:
        clip_title = info.clip_title
        filled.append("clip title")
    elif info.link.kind == CLIP and info.online:
        notes.append("The clip has the stream's title, so the clip title was left as it was.")
    if info.date:
        filled.append("date")
    if info.stream_url:
        filled.append("stream link")
    selection = replace(s, streamer_id=streamer_id, participant_ids=tuple(participants), game_id=game_id,
                        clip_title=clip_title, date=info.date or s.date, stream_url=info.stream_url or s.stream_url)
    metadata = regenerate(replace(current, selection=selection), library)
    if not info.online:
        notes.append("Twitch could not be reached, so only the channel from the link was used."
                     + (f" ({info.error})" if info.error else ""))
    return Filled(metadata, tuple(filled), tuple(added), tuple(notes))


def regenerate(metadata: ClipMetadata, library: Library) -> ClipMetadata:
    """New title, description and tags for the selection; hand edits are kept."""
    generated = generate(library, metadata.selection)
    if not metadata.selection.clip_title.strip():
        generated = replace(generated, title="")  # a title without what happens would only be in the way
    keep = tuple(metadata.edited_fields()) if metadata.generated is not None else tuple(
        f for f in ("title", "description", "tags") if getattr(metadata, f))
    return metadata.with_generated(generated, keep)


__all__ = ["CLIP", "VIDEO", "CHANNEL", "TwitchLink", "TwitchInfo", "TwitchLinkError", "Filled", "parse_link",
           "fetch_info", "offline_info", "apply_info", "regenerate", "mentions_in", "video_url", "channel_url"]

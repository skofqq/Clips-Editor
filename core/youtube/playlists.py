"""The streamer's playlist: every uploaded clip of a streamer goes into a
playlist named after them, made on the channel the first time it is needed.

The playlist is looked up by title each time (one cheap list call), so
renaming or deleting it on YouTube never leaves a stale id behind. Adding
costs 50 quota units, creating another 50 (an upload costs 1600).
"""

from __future__ import annotations

import threading

from core.youtube.errors import YouTubeError

MAX_TITLE = 150  # YouTube's limit for a playlist title
_lock = threading.Lock()  # two uploads of a new streamer must not make two playlists


def playlist_name(person) -> str:
    """The playlist a streamer's clips go to ("" = none): the name set in the
    profile, else the streamer's name. ``person`` is a library Person or None."""
    if person is None or getattr(person, "playlist_off", False):
        return ""
    name = (getattr(person, "playlist", "") or "").strip() or (getattr(person, "name", "") or "").strip()
    return name[:MAX_TITLE]


def _key(title: str) -> str:
    return " ".join(title.casefold().split())


def find_or_create(client, title: str, privacy: str = "public") -> tuple[str, bool]:
    """Id of the channel's playlist called ``title`` (case and spaces ignored),
    made when there is none; and whether it was made now."""
    title = title.strip()[:MAX_TITLE]
    if not title:
        raise YouTubeError("No playlist name.")
    with _lock:
        wanted = _key(title)
        for playlist_id, name in client.my_playlists():
            if playlist_id and _key(name) == wanted:
                return playlist_id, False
        playlist_id = client.create_playlist(title, privacy=privacy)
        if not playlist_id:
            raise YouTubeError("YouTube did not create the playlist.")
        return playlist_id, True


def add_video(client, video_id: str, title: str) -> str:
    """Put the uploaded video into the playlist ``title``: "added" or "created"."""
    playlist_id, created = find_or_create(client, title)
    client.add_to_playlist(playlist_id, video_id)
    return "created" if created else "added"

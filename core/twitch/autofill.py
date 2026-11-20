"""Fill the project's metadata from a Twitch clip: streamer, game, title,
date and channel link. New streamers and games are added to the library
(when allowed) so the next clip of the same channel finds them."""

from __future__ import annotations

from dataclasses import dataclass, replace

from core.metadata.generate import ClipMetadata, generate
from core.metadata.library import GAME, STREAMER, Library, LibraryError
from core.twitch.clip import TwitchClip


@dataclass(frozen=True)
class AutofillResult:
    metadata: ClipMetadata
    added: tuple[str, ...] = ()  # "Streamer “X”", "Game “Y”" created in the library
    missing: tuple[str, ...] = ()  # facts Twitch did not give (e.g. "game")


def _streamer(library: Library, clip: TwitchClip, add: bool) -> tuple[str, str]:
    """(profile id, added label)."""
    found = (library.find(STREAMER, clip.login) if clip.login else None) or library.find(STREAMER, clip.channel)
    if found is not None:
        # Complete an existing profile without overwriting what the user typed.
        updates = {}
        if clip.login and not found.username:
            updates["username"] = clip.login
        if clip.channel_url and not found.twitch_url:
            updates["twitch_url"] = clip.channel_url
        if updates:
            try:
                found = library.save(replace(found, **updates))
            except LibraryError:
                pass
        return found.id, ""
    if not add or not clip.channel:
        return "", ""
    created = library.create(STREAMER, clip.channel, username=clip.login, twitch_url=clip.channel_url)
    return created.id, f"Streamer “{created.name}”"


def _game(library: Library, clip: TwitchClip, add: bool) -> tuple[str, str]:
    if not clip.game:
        return "", ""
    found = library.find(GAME, clip.game)
    if found is not None:
        return found.id, ""
    if not add:
        return "", ""
    created = library.create(GAME, clip.game)
    return created.id, f"Game “{created.name}”"


def autofill(metadata: ClipMetadata, clip: TwitchClip, library: Library, *, add_to_library: bool = True
             ) -> AutofillResult:
    """New metadata for ``clip``; fields the user edited by hand are kept."""
    selection = metadata.selection
    added = []
    streamer_id, label = _streamer(library, clip, add_to_library)
    if label:
        added.append(label)
    game_id, label = _game(library, clip, add_to_library)
    if label:
        added.append(label)
    selection = replace(
        selection,
        streamer_id=streamer_id or selection.streamer_id,
        game_id=game_id or selection.game_id,
        clip_title=clip.title or selection.clip_title,
        date=clip.created or selection.date,
        stream_url=clip.channel_url or selection.stream_url,
    )
    updated = replace(metadata, selection=selection)
    updated = updated.with_generated(generate(library, selection), keep=tuple(metadata.edited_fields()))
    missing = tuple(name for name, value in (("streamer", streamer_id), ("game", game_id), ("title", clip.title))
                    if not value)
    return AutofillResult(updated, tuple(added), missing)

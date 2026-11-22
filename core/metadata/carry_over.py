"""Copy from the previous clip: a new clip of a streamer starts with what that
streamer's last clip had (participants, game, stream type, template, extra
tags and hashtags, own variables), then the description and tags are
generated from it. The clip title, date and stream link are the clip's own
and are not copied; the title is left for the user until the clip title is in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from core.metadata.generate import ClipMetadata, MetadataSelection, generate
from core.metadata.library import STREAMER, Library, normalize


@dataclass(frozen=True)
class CarryOver:
    metadata: ClipMetadata
    source: str  # the clip it was copied from
    streamer_id: str


def _key(path) -> str:
    return str(Path(path)).replace("\\", "/").casefold()


def last_clip_of(memory, streamer_id: str = "", exclude: str = ""):
    """The streamer's most recently edited clip in the clip memory ("" = any streamer's)."""
    best = None
    for entry in memory.clips():
        s = entry.metadata.selection
        if not s.streamer_id or (streamer_id and s.streamer_id != streamer_id):
            continue
        if exclude and _key(entry.source) == _key(exclude):
            continue
        if best is None or entry.updated_at > best.updated_at:
            best = entry
    return best


def guess_streamer(library: Library, file_name: str) -> str:
    """Streamer whose name or username is a word of the clip's file name ("" = none)."""
    words = {normalize(w) for w in re.split(r"[\W_]+", Path(file_name).stem) if w}
    stem = normalize(re.sub(r"[_\-.]+", " ", Path(file_name).stem))
    found = []
    for person in library.all(STREAMER):
        for label in person.labels():
            key = normalize(label)
            if len(key) >= 3 and (key in words or (" " in key and f" {key} " in f" {stem} ")):
                found.append(person.id)
                break
    return found[0] if len(found) == 1 else ""


def fresh(metadata: ClipMetadata) -> bool:
    """Nothing but maybe the streamer and the clip's own facts is filled in: safe to fill the rest."""
    s = metadata.selection
    return not (s.participant_ids or s.game_id or s.stream_type_ids or s.template_id or s.custom
                or s.project_tags or s.extra_hashtags or metadata.title.strip()
                or metadata.description.strip() or metadata.tags)


def _copyable(metadata: ClipMetadata) -> bool:
    s = metadata.selection
    return bool(s.participant_ids or s.game_id or s.stream_type_ids or s.template_id or s.custom
                or s.project_tags or s.extra_hashtags or metadata.description.strip() or metadata.tags)


def carry_over(current: ClipMetadata, previous: ClipMetadata, library: Library) -> ClipMetadata:
    """``current`` with the choices of ``previous`` and a new description and tags."""
    p, c = previous.selection, current.selection
    selection = MetadataSelection(
        streamer_id=c.streamer_id or p.streamer_id,
        participant_ids=c.participant_ids or p.participant_ids,
        game_id=c.game_id or p.game_id,
        stream_type_ids=c.stream_type_ids or p.stream_type_ids,
        template_id=c.template_id or p.template_id,
        clip_title=c.clip_title,
        date=c.date,
        stream_url=c.stream_url,
        custom=c.custom or p.custom,
        project_tags=c.project_tags or p.project_tags,
        extra_hashtags=c.extra_hashtags or p.extra_hashtags,
    )
    generated = generate(library, selection)
    if not selection.clip_title.strip():
        generated = replace(generated, title="")  # a title without what happens would only be in the way
    result = replace(current, selection=selection).with_generated(generated)
    # Hand edits of the last clip's description and tags are the user's own style: keep them,
    # unless the description talks about that clip (has its clip title).
    edited = previous.edited_fields()
    own_title = p.clip_title.strip().casefold()
    if "description" in edited and previous.description.strip() and not (
            own_title and own_title in previous.description.casefold()):
        result = replace(result, description=previous.description)
    if "tags" in edited and previous.tags:
        result = replace(result, tags=previous.tags)
    return result


def from_last_clip(current: ClipMetadata, memory, library: Library, source: str = "",
                   streamer_id: str = "") -> CarryOver | None:
    """Fill ``current`` from the streamer's last clip; None when there is nothing to copy."""
    streamer_id = streamer_id or current.selection.streamer_id
    if not streamer_id or not fresh(current):
        return None
    entry = last_clip_of(memory, streamer_id, exclude=source)
    if entry is None or not _copyable(entry.metadata):
        return None
    base = replace(current, selection=replace(current.selection, streamer_id=streamer_id))
    return CarryOver(carry_over(base, entry.metadata, library), entry.source, streamer_id)

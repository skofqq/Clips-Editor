"""Combine profiles and a template into title, description, tags and hashtags.

``ClipMetadata`` is what a project stores: the choices (``MetadataSelection``),
what was generated last time and the current, possibly hand-edited, fields.
A field counts as edited when it differs from what was generated, so a new
generation can keep those fields instead of silently overwriting them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date as _date
from typing import Any

from core.metadata.library import (
    GAME,
    PARTICIPANT,
    STREAM_TYPE,
    STREAMER,
    TEMPLATE,
    Library,
    Person,
    Template,
    clean_tags,
)
from core.metadata.template import BUILTIN_VARIABLES, render, render_title, unknown_variables, variables_in
from core.youtube.metadata import MAX_TAGS_CHARS, tags_length

FIELDS = ("title", "description", "tags")
_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def hashtag(text: str) -> str:
    """``GTA VI`` → ``#GTAVI``; characters YouTube does not link are dropped."""
    body = re.sub(r"[^\w]", "", text.lstrip("#"), flags=re.UNICODE)
    return f"#{body}" if body else ""


def parse_custom(text: str) -> dict[str, str]:
    """``name = value`` per line → user-defined variables."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip().strip("{}")
        if _VAR_NAME.match(name):
            values[name] = value.strip().replace("\\n", "\n")
    return values


def format_custom(values: dict[str, str]) -> str:
    return "\n".join(f"{k} = {v}".replace("\n", "\\n") for k, v in values.items())


def fit_tags(tags: tuple[str, ...], limit: int = MAX_TAGS_CHARS) -> tuple[str, ...]:
    """Drop tags from the end until YouTube's 500 character limit fits."""
    kept = list(tags)
    while kept and tags_length(tuple(kept)) > limit:
        kept.pop()
    return tuple(kept)


@dataclass(frozen=True)
class MetadataSelection:
    streamer_id: str = ""
    participant_ids: tuple[str, ...] = ()
    game_id: str = ""
    stream_type_ids: tuple[str, ...] = ()
    template_id: str = ""  # "" = the profiles' or the library's default
    clip_title: str = ""
    date: str = ""  # "" = today
    stream_url: str = ""
    custom: tuple[tuple[str, str], ...] = ()  # user-defined variables
    project_tags: tuple[str, ...] = ()
    extra_hashtags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "streamer_id": self.streamer_id,
            "participant_ids": list(self.participant_ids),
            "game_id": self.game_id,
            "stream_type_ids": list(self.stream_type_ids),
            "template_id": self.template_id,
            "clip_title": self.clip_title,
            "date": self.date,
            "stream_url": self.stream_url,
            "custom": [list(p) for p in self.custom],
            "project_tags": list(self.project_tags),
            "extra_hashtags": list(self.extra_hashtags),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MetadataSelection":
        def strs(key):
            return tuple(str(x) for x in data.get(key) or () if str(x))

        custom = tuple(
            (str(p[0]), str(p[1])) for p in data.get("custom") or ()
            if isinstance(p, (list, tuple)) and len(p) == 2
        )
        return cls(
            streamer_id=str(data.get("streamer_id") or ""),
            participant_ids=strs("participant_ids"),
            game_id=str(data.get("game_id") or ""),
            stream_type_ids=strs("stream_type_ids"),
            template_id=str(data.get("template_id") or ""),
            clip_title=str(data.get("clip_title") or ""),
            date=str(data.get("date") or ""),
            stream_url=str(data.get("stream_url") or ""),
            custom=custom,
            project_tags=clean_tags(strs("project_tags")),
            extra_hashtags=strs("extra_hashtags"),
        )


@dataclass(frozen=True)
class GeneratedMetadata:
    title: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    hashtags: tuple[str, ...] = ()
    category_id: str = ""
    template_id: str = ""
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "description": self.description, "tags": list(self.tags),
            "hashtags": list(self.hashtags), "category_id": self.category_id, "template_id": self.template_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GeneratedMetadata":
        return cls(
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            tags=clean_tags(data.get("tags") or ()),
            hashtags=tuple(str(h) for h in data.get("hashtags") or ()),
            category_id=str(data.get("category_id") or ""),
            template_id=str(data.get("template_id") or ""),
        )


def choose_template(library: Library, selection: MetadataSelection) -> Template | None:
    """Explicit choice, else the streamer's, game's or stream type's own, else the default."""
    candidates = [selection.template_id]
    streamer = library.get(STREAMER, selection.streamer_id)
    game = library.get(GAME, selection.game_id)
    candidates += [streamer.template_id if streamer else "", game.template_id if game else ""]
    candidates += [s.template_id for s in library.many(STREAM_TYPE, selection.stream_type_ids)]
    for template_id in candidates:
        template = library.get(TEMPLATE, template_id) if template_id else None
        if template is not None:
            return template
    return library.default_template()


def _person_line(person: Person) -> str:
    url = person.main_url
    return f"{person.name} — {url}" if url else person.name


def variables(library: Library, selection: MetadataSelection, template: Template | None = None) -> dict[str, str]:
    streamer = library.get(STREAMER, selection.streamer_id)
    participants = library.many(PARTICIPANT, selection.participant_ids)
    game = library.get(GAME, selection.game_id)
    stream_types = library.many(STREAM_TYPE, selection.stream_type_ids)
    hashtags = _hashtags(template, selection)
    values = {
        "streamer": streamer.name if streamer else "",
        "streamer_username": streamer.username if streamer else "",
        "streamer_channel_url": (streamer.youtube_url or streamer.twitch_url) if streamer else "",
        "streamer_twitch_url": streamer.twitch_url if streamer else "",
        "game": game.name if game else "",
        "stream_type": ", ".join(s.name for s in stream_types),
        "participants": ", ".join(p.name for p in participants),
        "participants_links": "\n".join(_person_line(p) for p in participants),
        "stream_url": selection.stream_url or (streamer.twitch_url if streamer else ""),
        "clip_title": selection.clip_title.strip(),
        "date": selection.date.strip() or _date.today().strftime("%d.%m.%Y"),
        "hashtags": " ".join(hashtags),
    }
    for name, value in selection.custom:
        if name not in BUILTIN_VARIABLES:
            values[name] = value
    return values


def _hashtags(template: Template | None, selection: MetadataSelection) -> tuple[str, ...]:
    raw = list(template.hashtags if template else ()) + list(selection.extra_hashtags)
    seen: dict[str, str] = {}
    for item in raw:
        tag = hashtag(item)
        if tag and tag.casefold() not in seen:
            seen[tag.casefold()] = tag
    return tuple(seen.values())


def collect_tags(library: Library, selection: MetadataSelection, template: Template | None) -> tuple[str, ...]:
    """Streamer first, then game, participants, stream types, template and project tags; no duplicates."""
    tags: list[str] = []
    streamer = library.get(STREAMER, selection.streamer_id)
    if streamer is not None:
        tags += [streamer.name, *streamer.default_tags]
    game = library.get(GAME, selection.game_id)
    if game is not None:
        tags += [game.name, *game.default_tags]
    for person in library.many(PARTICIPANT, selection.participant_ids):
        tags += [person.name, *person.default_tags]
    for stream_type in library.many(STREAM_TYPE, selection.stream_type_ids):
        tags += list(stream_type.default_tags)
    if template is not None:
        tags += list(template.default_tags)
    tags += list(selection.project_tags)
    return fit_tags(clean_tags(tags))


def generate(library: Library, selection: MetadataSelection) -> GeneratedMetadata:
    template = choose_template(library, selection)
    values = variables(library, selection, template)
    title_tpl = template.title if template else "{clip_title}"
    description_tpl = template.description if template else ""
    known = set(values)
    warnings = [f"Unknown variable {{{v}}}" for v in unknown_variables(title_tpl + description_tpl, known)]
    empty = [v for v in variables_in(title_tpl) if v in known and not values[v].strip()]
    if empty:
        warnings.append("Empty in the title: " + ", ".join("{" + v + "}" for v in empty))
    return GeneratedMetadata(
        title=render_title(title_tpl, values),
        description=render(description_tpl, values),
        tags=collect_tags(library, selection, template),
        hashtags=_hashtags(template, selection),
        category_id=template.category_id if template else "",
        template_id=template.id if template else "",
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class ClipMetadata:
    selection: MetadataSelection = field(default_factory=MetadataSelection)
    generated: GeneratedMetadata | None = None  # last generation, to spot manual edits
    title: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return self == ClipMetadata()

    def edited_fields(self) -> list[str]:
        if self.generated is None:
            return [f for f in FIELDS if getattr(self, f)]
        return [f for f in FIELDS if getattr(self, f) != getattr(self.generated, f)]

    def with_generated(self, generated: GeneratedMetadata, keep: tuple[str, ...] = ()) -> "ClipMetadata":
        """New generation; fields named in ``keep`` keep their current (edited) value."""
        values = {f: getattr(self, f) if f in keep else getattr(generated, f) for f in FIELDS}
        return replace(self, generated=generated, **values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection": self.selection.to_dict(),
            "generated": self.generated.to_dict() if self.generated else None,
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ClipMetadata":
        if not isinstance(data, dict):
            raise ValueError("metadata is not an object")
        generated = data.get("generated")
        return cls(
            selection=MetadataSelection.from_dict(data.get("selection") or {}),
            generated=GeneratedMetadata.from_dict(generated) if isinstance(generated, dict) else None,
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            tags=clean_tags(data.get("tags") or ()),
        )

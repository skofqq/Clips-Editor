"""Several titles to choose from: the clip title written in different ways
(as typed, in capitals, streamer first, with the game, with an emoji, with
the participants), each run through the clip's template, plus the clip
title through every other template in the library.

No network and no AI: the variants come from what the Data page already
knows, so they work offline and keep the channel's template.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from core.metadata.generate import GeneratedMetadata, MetadataSelection, choose_template, generate
from core.metadata.library import GAME, STREAMER, TEMPLATE, Library
from core.youtube.metadata import MAX_TITLE as MAX_TITLE_CHARS

AS_TYPED, CAPS, STREAMER_FIRST, WITH_GAME, WITH_EMOJI, WITH_PEOPLE, TEMPLATE_PREFIX = (
    "as_typed", "caps", "streamer_first", "with_game", "with_emoji", "with_people", "template:")
LABELS = {AS_TYPED: "As typed", CAPS: "Capitals", STREAMER_FIRST: "Streamer first", WITH_GAME: "With the game",
          WITH_EMOJI: "With an emoji", WITH_PEOPLE: "With who is in it"}
DEFAULT_EMOJI = "😱"


@dataclass(frozen=True)
class TitleVariant:
    key: str  # how it was made (AS_TYPED, CAPS, … or "template:<id>")
    label: str
    metadata: GeneratedMetadata

    @property
    def title(self) -> str:
        return self.metadata.title


def _clip_titles(library: Library, selection: MetadataSelection) -> list[tuple[str, str]]:
    clip = selection.clip_title.strip()
    if not clip:
        return []
    from core.subtitles.emoji import emoji_for

    streamer = library.person(selection.streamer_id, STREAMER)
    game = library.get(GAME, selection.game_id) if selection.game_id else None
    people = [p.name for p in library.people(selection.participant_ids) if p.name][:2]
    result = [(AS_TYPED, clip), (CAPS, clip.upper())]
    if streamer is not None and streamer.name and streamer.name.casefold() not in clip.casefold():
        result.append((STREAMER_FIRST, f"{streamer.name}: {clip}"))
    if game is not None and game.name and game.name.casefold() not in clip.casefold():
        result.append((WITH_GAME, f"{clip} в {game.name}"))
    if not any(ord(ch) > 0x2600 for ch in clip):
        result.append((WITH_EMOJI, f"{clip} {emoji_for(clip.split()) or DEFAULT_EMOJI}"))
    if people and streamer is not None and streamer.name:
        result.append((WITH_PEOPLE, f"{streamer.name} и {' и '.join(people)}: {clip}"))
    return result


def title_variants(library: Library, selection: MetadataSelection, limit: int = 8) -> list[TitleVariant]:
    """Different titles for the same clip; the first is what Generate gives. Too long and repeated ones are dropped."""
    variants: list[TitleVariant] = []
    seen: set[str] = set()

    def add(key: str, label: str, generated: GeneratedMetadata) -> None:
        title = generated.title.strip()
        if title and title not in seen and len(title) <= MAX_TITLE_CHARS and len(variants) < limit:
            seen.add(title)
            variants.append(TitleVariant(key, label, generated))

    for key, clip in _clip_titles(library, selection):
        add(key, LABELS[key], generate(library, replace(selection, clip_title=clip)))
    current = choose_template(library, selection)
    for template in library.all(TEMPLATE):
        if current is not None and template.id == current.id:
            continue
        add(TEMPLATE_PREFIX + template.id, f"Template “{template.name}”",
            generate(library, replace(selection, template_id=template.id)))
    return variants

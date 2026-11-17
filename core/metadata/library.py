"""Reusable metadata library in SQLite: streamers, participants, games,
stream types and title/description templates.

Every entity is a small immutable dataclass. Rows keep the searchable
columns (id, name, a normalised key) and the rest as JSON, so new optional
fields do not need a schema change. Schema changes go through ``MIGRATIONS``
(``PRAGMA user_version``). Nothing secret is stored here.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

STREAMER, PARTICIPANT, GAME, STREAM_TYPE, TEMPLATE = "streamer", "participant", "game", "stream_type", "template"
KINDS = (STREAMER, PARTICIPANT, GAME, STREAM_TYPE, TEMPLATE)
TABLES = {
    STREAMER: "streamers",
    PARTICIPANT: "participants",
    GAME: "games",
    STREAM_TYPE: "stream_types",
    TEMPLATE: "templates",
}


class LibraryError(ValueError):
    pass


def normalize(text: str) -> str:
    """Key used to spot duplicates: case, spaces and a leading @ do not matter."""
    text = text.strip().lstrip("@").casefold()
    return re.sub(r"\s+", " ", text)


def clean_tags(tags: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for raw in tags:
        tag = " ".join(str(raw).split()).lstrip("#").strip()
        if tag and tag.casefold() not in seen:
            seen[tag.casefold()] = tag
    return tuple(seen.values())


def _links(value: Any) -> tuple[tuple[str, str], ...]:
    result = []
    for item in value or ():
        if isinstance(item, (list, tuple)) and len(item) == 2:
            label, url = str(item[0]).strip(), str(item[1]).strip()
            if url:
                result.append((label, url))
    return tuple(result)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------- entities
@dataclass(frozen=True)
class Person:
    """A streamer or a participant."""

    id: str
    name: str
    username: str = ""
    youtube_url: str = ""
    twitch_url: str = ""
    social_links: tuple[tuple[str, str], ...] = ()  # (label, url)
    default_tags: tuple[str, ...] = ()
    template_id: str = ""  # streamer's own template, optional
    kind: str = PARTICIPANT

    @property
    def key(self) -> str:
        return normalize(self.username or self.name)

    @property
    def main_url(self) -> str:
        return self.youtube_url or self.twitch_url or (self.social_links[0][1] if self.social_links else "")

    def labels(self) -> tuple[str, ...]:
        return tuple(x for x in (self.name, self.username) if x)


@dataclass(frozen=True)
class Game:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    default_tags: tuple[str, ...] = ()
    template_id: str = ""
    kind: str = GAME

    @property
    def key(self) -> str:
        return normalize(self.name)

    def labels(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass(frozen=True)
class StreamType:
    id: str
    name: str
    default_tags: tuple[str, ...] = ()
    template_id: str = ""
    kind: str = STREAM_TYPE

    @property
    def key(self) -> str:
        return normalize(self.name)

    def labels(self) -> tuple[str, ...]:
        return (self.name,)


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    title: str = "{clip_title}"
    description: str = ""
    default_tags: tuple[str, ...] = ()
    hashtags: tuple[str, ...] = ()  # written into the description as #tags, not sent as YouTube tags
    category_id: str = ""  # "" keeps the upload dialog's choice
    is_default: bool = False
    kind: str = TEMPLATE

    @property
    def key(self) -> str:
        return normalize(self.name)

    def labels(self) -> tuple[str, ...]:
        return (self.name,)


Entity = Person | Game | StreamType | Template

_CLASSES = {STREAMER: Person, PARTICIPANT: Person, GAME: Game, STREAM_TYPE: StreamType, TEMPLATE: Template}


def entity_from_dict(kind: str, data: dict) -> Entity:
    cls = _CLASSES[kind]
    names = {f.name for f in fields(cls)}
    values = {k: v for k, v in data.items() if k in names}
    values["kind"] = kind
    values["id"] = str(values.get("id") or new_id())
    values["name"] = str(values.get("name") or "").strip()
    for key in ("default_tags",):
        if key in names:
            values[key] = clean_tags(values.get(key) or ())
    if "hashtags" in names:
        values["hashtags"] = clean_tags(values.get("hashtags") or ())
    if "aliases" in names:
        values["aliases"] = tuple(a.strip() for a in values.get("aliases") or () if str(a).strip())
    if "social_links" in names:
        values["social_links"] = _links(values.get("social_links"))
    for f in fields(cls):
        if f.type in ("str",) and f.name in values and not isinstance(values[f.name], str):
            values[f.name] = str(values[f.name] or "")
    if "is_default" in names:
        values["is_default"] = bool(values.get("is_default"))
    return cls(**values)


def entity_to_dict(entity: Entity) -> dict:
    data = asdict(entity)
    data.pop("kind", None)
    return data


# ----------------------------------------------------------------- seeds
DEFAULT_DESCRIPTION = """🔥 {clip_title}

Streamer: {streamer}
[[Channel: {streamer_channel_url}
]]Game: {game}
Stream type: {stream_type}

[[Participants:
{participants_links}

]][[Full stream: {stream_url}

]]{hashtags}"""

SEED_TEMPLATES = (
    Template("tpl-shorts", "Streamer — clip #shorts", "{streamer} — {clip_title} #shorts", DEFAULT_DESCRIPTION,
             ("twitch", "shorts", "twitch clips"), ("shorts", "twitch"), "20", True),
    Template("tpl-plays", "Streamer plays game", "{streamer} plays {game}[[ with {participants}]]",
             DEFAULT_DESCRIPTION, ("twitch", "gaming"), ("shorts", "gaming"), "20"),
    Template("tpl-clip-first", "Clip | streamer", "{clip_title} | {streamer} #shorts", DEFAULT_DESCRIPTION,
             ("twitch", "shorts"), ("shorts",), "20"),
)
SEED_STREAM_TYPES = tuple(
    StreamType(f"st-{i}", name, tags) for i, (name, tags) in enumerate((
        ("Just Chatting", ("just chatting",)),
        ("Gaming", ("gaming",)),
        ("IRL", ("irl",)),
        ("Multiplayer", ("multiplayer",)),
        ("Challenge", ("challenge",)),
    ), start=1)
)
SEED_GAMES = (
    Game("game-gta5", "GTA V", ("GTA 5", "Grand Theft Auto V"), ("gta 5", "gta v")),
    Game("game-gta6", "GTA VI", ("GTA 6", "Grand Theft Auto VI"), ("gta 6", "gta vi")),
    Game("game-minecraft", "Minecraft", ("Майнкрафт",), ("minecraft",)),
    Game("game-cs2", "CS2", ("Counter-Strike 2", "КС2"), ("cs2", "counter strike 2")),
)


# ------------------------------------------------------------- migrations
def _migration_1(db: sqlite3.Connection) -> None:
    for table in TABLES.values():
        db.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_key ON {table}(key)")


def _migration_2(db: sqlite3.Connection) -> None:
    for kind, items in ((TEMPLATE, SEED_TEMPLATES), (STREAM_TYPE, SEED_STREAM_TYPES), (GAME, SEED_GAMES)):
        for item in items:
            db.execute(
                f"INSERT OR IGNORE INTO {TABLES[kind]}(id, name, key, data) VALUES (?, ?, ?, ?)",
                (item.id, item.name, item.key, json.dumps(entity_to_dict(item), ensure_ascii=False)),
            )


MIGRATIONS = (_migration_1, _migration_2)


# ---------------------------------------------------------------- library
class Library:
    """The user's profiles and templates. ``path=None`` keeps them in memory."""

    def __init__(self, path: Path | str | None) -> None:
        self.path = Path(path) if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path) if self.path else ":memory:", check_same_thread=False)
        self._migrate()

    def close(self) -> None:
        self._db.close()

    @property
    def version(self) -> int:
        return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        with self._lock:
            current = self.version
            for number, migration in enumerate(MIGRATIONS, start=1):
                if number <= current:
                    continue
                with self._db:
                    migration(self._db)
                    self._db.execute(f"PRAGMA user_version = {number}")
                log.info("Metadata library migrated to version %d", number)

    # ------------------------------------------------------------ reading
    def all(self, kind: str) -> list[Entity]:
        rows = self._db.execute(f"SELECT data FROM {TABLES[kind]} ORDER BY name COLLATE NOCASE").fetchall()
        result = []
        for (data,) in rows:
            try:
                result.append(entity_from_dict(kind, json.loads(data)))
            except (TypeError, ValueError) as exc:
                log.warning("Skipping a broken %s entry: %s", kind, exc)
        return result

    def get(self, kind: str, entity_id: str) -> Entity | None:
        if not entity_id:
            return None
        row = self._db.execute(f"SELECT data FROM {TABLES[kind]} WHERE id = ?", (entity_id,)).fetchone()
        return entity_from_dict(kind, json.loads(row[0])) if row else None

    def many(self, kind: str, ids: Iterable[str]) -> list[Entity]:
        return [e for e in (self.get(kind, i) for i in ids) if e is not None]

    def find(self, kind: str, text: str) -> Entity | None:
        """Profile whose name, username or alias matches ``text``."""
        wanted = normalize(text)
        if not wanted:
            return None
        return next((e for e in self.all(kind) if wanted in {normalize(x) for x in e.labels()}), None)

    def default_template(self) -> Template | None:
        templates = self.all(TEMPLATE)
        return next((t for t in templates if t.is_default), templates[0] if templates else None)

    # ------------------------------------------------------------ writing
    def save(self, entity: Entity) -> Entity:
        """Insert or update. Raises LibraryError on an empty name or a duplicate."""
        kind = entity.kind
        if not entity.name.strip():
            raise LibraryError("The name is empty.")
        entity = entity_from_dict(kind, entity_to_dict(entity))  # cleaned
        table = TABLES[kind]
        with self._lock:
            clash = self._db.execute(
                f"SELECT name FROM {table} WHERE key = ? AND id != ?", (entity.key, entity.id)
            ).fetchone()
            if clash:
                raise LibraryError(f"“{clash[0]}” already exists.")
            with self._db:
                if kind == TEMPLATE and entity.is_default:
                    self._clear_default(entity.id)
                self._db.execute(
                    f"INSERT INTO {table}(id, name, key, data) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, key=excluded.key, data=excluded.data, "
                    "updated_at=CURRENT_TIMESTAMP",
                    (entity.id, entity.name, entity.key, json.dumps(entity_to_dict(entity), ensure_ascii=False)),
                )
        return entity

    def _clear_default(self, keep_id: str) -> None:
        for (entity_id, data) in self._db.execute(f"SELECT id, data FROM {TABLES[TEMPLATE]}").fetchall():
            values = json.loads(data)
            if entity_id != keep_id and values.get("is_default"):
                values["is_default"] = False
                self._db.execute(
                    f"UPDATE {TABLES[TEMPLATE]} SET data = ? WHERE id = ?",
                    (json.dumps(values, ensure_ascii=False), entity_id),
                )

    def delete(self, kind: str, entity_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(f"DELETE FROM {TABLES[kind]} WHERE id = ?", (entity_id,))

    def create(self, kind: str, name: str, **values) -> Entity:
        """New profile, or the existing one with the same name/username."""
        existing = self.find(kind, values.get("username") or name) or self.find(kind, name)
        if existing is not None:
            return existing
        return self.save(entity_from_dict(kind, {"name": name, **values}))

    def set_default_template(self, template_id: str) -> None:
        template = self.get(TEMPLATE, template_id)
        if template is not None:
            self.save(replace(template, is_default=True))


def blank(kind: str, name: str = "") -> Entity:
    return entity_from_dict(kind, {"id": new_id(), "name": name})


def kind_label(kind: str) -> str:
    return {
        STREAMER: "Streamer", PARTICIPANT: "Participant", GAME: "Game",
        STREAM_TYPE: "Stream type", TEMPLATE: "Template",
    }[kind]


__all__ = [
    "Entity", "Game", "KINDS", "Library", "LibraryError", "PARTICIPANT", "Person", "STREAMER", "STREAM_TYPE",
    "StreamType", "TEMPLATE", "Template", "GAME", "blank", "clean_tags", "entity_from_dict", "entity_to_dict",
    "kind_label", "new_id", "normalize",
]

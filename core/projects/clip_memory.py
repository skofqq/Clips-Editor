"""Clip memory: each source clip keeps its title, description, tags and cover,
and every place it was published to.

Stored as one JSON file in the user data folder, keyed by the clip's path
(with the file name and size as a fallback when the clip was moved). Cover
pictures of published Shorts are kept as JPEG files next to it.

The project still holds the metadata of the clip being edited; this store is
what brings it back when the same clip is opened again, in any project.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from core.metadata.generate import ClipMetadata
from core.thumbnails.model import ThumbnailDesign

log = logging.getLogger(__name__)

YOUTUBE, TIKTOK, INSTAGRAM = "youtube", "tiktok", "instagram"
SERVICE_TEXT = {YOUTUBE: "YouTube", TIKTOK: "TikTok", INSTAGRAM: "Instagram Reels"}
VERSION = 1


def clip_key(path: str | Path) -> str:
    return str(Path(path)).replace("\\", "/").casefold()


def _file_id(path: str | Path) -> str:
    """"name|size" of an existing file: finds a clip that was moved to another folder."""
    p = Path(path)
    try:
        return f"{p.name.casefold()}|{p.stat().st_size}"
    except OSError:
        return ""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Publication:
    """One upload or post of a Short."""

    service: str
    url: str
    title: str
    published_at: str
    source: str = ""  # the clip it was made from ("" = not known)
    video_id: str = ""
    channel: str = ""  # channel / account name
    privacy: str = ""
    file: str = ""  # the rendered file that was sent
    cover: str = ""  # JPEG of the cover in the covers folder ("" = none)

    @property
    def date_text(self) -> str:
        try:
            return datetime.fromisoformat(self.published_at).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return self.published_at

    @classmethod
    def from_dict(cls, data: dict) -> "Publication":
        return cls(**{k: str(data.get(k) or "") for k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ClipEntry:
    source: str
    metadata: ClipMetadata = field(default_factory=ClipMetadata)
    thumbnail: ThumbnailDesign = field(default_factory=ThumbnailDesign)
    updated_at: str = ""
    file_id: str = ""

    def to_dict(self) -> dict:
        return {"source": self.source, "metadata": self.metadata.to_dict(), "thumbnail": self.thumbnail.to_dict(),
                "updated_at": self.updated_at, "file_id": self.file_id}

    @classmethod
    def from_dict(cls, data: dict) -> "ClipEntry":
        try:
            metadata = ClipMetadata.from_dict(data.get("metadata") or {})
        except (KeyError, TypeError, ValueError):
            metadata = ClipMetadata()
        return cls(str(data.get("source") or ""), metadata, ThumbnailDesign.from_dict(data.get("thumbnail")),
                   str(data.get("updated_at") or ""), str(data.get("file_id") or ""))


class ClipMemory:
    def __init__(self, path: Path, covers_dir: Path | None = None) -> None:
        self.path = Path(path)
        self.covers_dir = Path(covers_dir) if covers_dir is not None else self.path.parent / "covers"
        self._clips: dict[str, ClipEntry] = {}
        self._published: list[Publication] = []
        self.migrated = False  # old upload log and projects were read in
        self._load()

    # ------------------------------------------------------------ file
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Could not read the clip memory: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        self.migrated = bool(raw.get("migrated"))
        for item in raw.get("clips") or []:
            if isinstance(item, dict):
                entry = ClipEntry.from_dict(item)
                if entry.source:
                    self._clips[clip_key(entry.source)] = entry
        for item in raw.get("published") or []:
            if isinstance(item, dict):
                self._published.append(Publication.from_dict(item))

    def save(self) -> None:
        data = {"version": VERSION, "migrated": self.migrated,
                "clips": [e.to_dict() for e in self._clips.values()],
                "published": [asdict(p) for p in self._published]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("Could not save the clip memory: %s", exc)

    # ------------------------------------------------------------ clips
    def get(self, source: str | Path) -> ClipEntry | None:
        entry = self._clips.get(clip_key(source))
        if entry is not None:
            return entry
        file_id = _file_id(source)
        if file_id:
            for entry in self._clips.values():
                if entry.file_id == file_id:
                    return entry
        return None

    def remember(self, source: str | Path, metadata: ClipMetadata, thumbnail: ThumbnailDesign,
                 save: bool = True) -> bool:
        """Store what the clip has now; an empty clip is not stored. True when something changed."""
        old = self.get(source)
        if old is None and metadata.empty and thumbnail == ThumbnailDesign():
            return False
        if old is not None and old.metadata == metadata and old.thumbnail == thumbnail:
            return False
        if old is not None and clip_key(old.source) != clip_key(source):
            self._clips.pop(clip_key(old.source), None)  # the clip moved: follow it
        self._clips[clip_key(source)] = ClipEntry(str(source), metadata, thumbnail, _now(),
                                                  _file_id(source) or (old.file_id if old else ""))
        if save:
            self.save()
        return True

    def clips(self) -> list[ClipEntry]:
        return list(self._clips.values())

    # ------------------------------------------------------------ publications
    def add_publication(self, publication: Publication, save: bool = True) -> Publication:
        if publication.video_id and any(p.service == publication.service and p.video_id == publication.video_id
                                        for p in self._published):
            return publication
        self._published.append(publication)
        if save:
            self.save()
        return publication

    def set_cover(self, publication: Publication, jpeg: bytes) -> Publication:
        """Keep the cover picture of ``publication`` and point it there."""
        name = re.sub(r"[^\w-]", "_", f"{publication.service}_{publication.video_id or publication.published_at}")
        path = self.covers_dir / f"{name}.jpg"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(jpeg)
        except OSError as exc:
            log.warning("Could not keep the cover: %s", exc)
            return publication
        updated = Publication(**{**asdict(publication), "cover": str(path)})
        self._published = [updated if p is publication or p == publication else p for p in self._published]
        self.save()
        return updated

    def publications(self, source: str | Path | None = None) -> list[Publication]:
        """Newest first; with ``source``, only that clip's (also the ones matched by title)."""
        items = sorted(self._published, key=lambda p: p.published_at, reverse=True)
        if source is None:
            return items
        key, entry = clip_key(source), self.get(source)
        title = entry.metadata.title.strip().casefold() if entry is not None else ""
        return [p for p in items if (p.source and clip_key(p.source) == key)
                or (entry is not None and p.source and clip_key(p.source) == clip_key(entry.source))
                or (not p.source and title and p.title.strip().casefold() == title)]

    def source_of(self, publication: Publication) -> str:
        """The clip a publication came from, matching old uploads by their title."""
        if publication.source:
            return publication.source
        title = publication.title.strip().casefold()
        if title:
            for entry in self._clips.values():
                if entry.metadata.title.strip().casefold() == title:
                    return entry.source
        return ""

    def cover_of(self, publication: Publication) -> str:
        return publication.cover if publication.cover and Path(publication.cover).is_file() else ""

    # ------------------------------------------------------------ migration
    def import_upload_log(self, records) -> int:
        """Old YouTube uploads (core.youtube.history records): titles and links, no clip or cover."""
        count = 0
        for r in records:
            if not r.video_id:
                continue
            before = len(self._published)
            self.add_publication(Publication(YOUTUBE, r.url, r.title, r.uploaded_at, "", r.video_id,
                                             r.channel_title, r.privacy, r.file), save=False)
            count += len(self._published) - before
        return count

    def import_project(self, project) -> int:
        """Title and cover of a saved project, for its clip (only one-clip projects are clear enough)."""
        media = [m.path for m in getattr(project, "media", ())]
        if len(media) != 1 or self.get(media[0]) is not None:
            return 0
        return int(self.remember(media[0], project.metadata, project.thumbnail, save=False))

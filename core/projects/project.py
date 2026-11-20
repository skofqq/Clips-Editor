"""Project model and the ``.tssproj`` file format (UTF-8 JSON).

Stores media references and the Stacked Layout composition; later phases
add timeline, audio edits and so on under their own keys. Unknown keys are
preserved on load so that older builds do not drop newer data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.censor.review import CensorState
from core.editor.composition import Composition
from core.highlights.finder import Highlight
from core.metadata.generate import ClipMetadata
from core.subtitles.model import SubtitleSettings
from core.timeline.model import Timeline
from core.transcription.model import Transcript

PROJECT_EXTENSION = ".tssproj"
SCHEMA_VERSION = 1


class ProjectFormatError(ValueError):
    pass


@dataclass
class MediaRef:
    path: str
    size_bytes: int = 0
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size_bytes": self.size_bytes, "duration_s": self.duration_s}

    @classmethod
    def from_dict(cls, data: dict) -> "MediaRef":
        if not isinstance(data, dict) or not isinstance(data.get("path"), str):
            raise ProjectFormatError("Media entry without a path.")
        return cls(
            path=data["path"],
            size_bytes=int(data.get("size_bytes") or 0),
            duration_s=float(data.get("duration_s") or 0.0),
        )


@dataclass
class Project:
    name: str = "Untitled"
    media: list[MediaRef] = field(default_factory=list)
    composition: Composition | None = None  # None until the first media sets a default
    timelines: dict[str, Timeline] = field(default_factory=dict)  # edits per media path
    transcripts: dict[str, Transcript] = field(default_factory=dict)  # per media path
    censors: dict[str, CensorState] = field(default_factory=dict)  # censor review per media path
    metadata: ClipMetadata = field(default_factory=ClipMetadata)  # YouTube title/description/tags choices
    subtitles: SubtitleSettings = field(default_factory=SubtitleSettings)  # caption style and edits
    highlights: dict[str, tuple[Highlight, ...]] = field(default_factory=dict)  # found moments per media path
    extra: dict[str, Any] = field(default_factory=dict)

    def add_media(self, ref: MediaRef) -> bool:
        """Add a media reference; returns False if it is already present."""
        key = _norm(ref.path)
        if any(_norm(m.path) == key for m in self.media):
            return False
        self.media.append(ref)
        return True

    def remove_media(self, path: str) -> None:
        key = _norm(path)
        self.media = [m for m in self.media if _norm(m.path) != key]
        for store in (self.timelines, self.transcripts, self.censors, self.highlights):
            for stored in [p for p in store if _norm(p) == key]:
                del store[stored]

    def timeline_for(self, path: str) -> Timeline | None:
        key = _norm(path)
        return next((t for p, t in self.timelines.items() if _norm(p) == key), None)

    def transcript_for(self, path: str) -> Transcript | None:
        key = _norm(path)
        return next((t for p, t in self.transcripts.items() if _norm(p) == key), None)

    def set_transcript(self, path: str, transcript: Transcript | None) -> None:
        key = _norm(path)
        for stored in [p for p in self.transcripts if _norm(p) == key]:
            del self.transcripts[stored]
        if transcript is not None:
            self.transcripts[path] = transcript

    def highlights_for(self, path: str) -> tuple[Highlight, ...] | None:
        key = _norm(path)
        return next((h for p, h in self.highlights.items() if _norm(p) == key), None)

    def set_highlights(self, path: str, highlights) -> None:
        key = _norm(path)
        for stored in [p for p in self.highlights if _norm(p) == key]:
            del self.highlights[stored]
        if highlights is not None:
            self.highlights[path] = tuple(highlights)

    def censor_for(self, path: str) -> CensorState | None:
        key = _norm(path)
        return next((c for p, c in self.censors.items() if _norm(p) == key), None)

    def set_censor(self, path: str, state: CensorState | None) -> None:
        key = _norm(path)
        for stored in [p for p in self.censors if _norm(p) == key]:
            del self.censors[stored]
        if state is not None and (state.suggestions or state.applied_mute_ids):
            self.censors[path] = state

    def set_timeline(self, path: str, timeline: Timeline) -> None:
        key = _norm(path)
        for stored in [p for p in self.timelines if _norm(p) == key and p != path]:
            del self.timelines[stored]
        self.timelines[path] = timeline

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.extra)
        data.update(
            {
                "schema_version": SCHEMA_VERSION,
                "name": self.name,
                "media": [m.to_dict() for m in self.media],
            }
        )
        if self.composition is not None:
            data["composition"] = self.composition.to_dict()
        if self.timelines:
            data["timelines"] = [
                {"media": path, **timeline.to_dict()} for path, timeline in self.timelines.items()
            ]
        if self.transcripts:
            data["transcripts"] = [
                {"media": path, **transcript.to_dict()} for path, transcript in self.transcripts.items()
            ]
        if self.censors:
            data["censor"] = [{"media": path, **state.to_dict()} for path, state in self.censors.items()]
        if not self.metadata.empty:
            data["metadata"] = self.metadata.to_dict()
        if self.highlights:
            data["highlights"] = [
                {"media": path, "items": [h.to_dict() for h in items]} for path, items in self.highlights.items()
            ]
        if self.subtitles != SubtitleSettings():
            data["subtitles"] = self.subtitles.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        if not isinstance(data, dict):
            raise ProjectFormatError("Project file is not a JSON object.")
        version = data.get("schema_version")
        if not isinstance(version, int) or version < 1:
            raise ProjectFormatError("Missing or invalid schema_version.")
        if version > SCHEMA_VERSION:
            raise ProjectFormatError(
                f"Project was saved by a newer version (schema {version})."
            )
        known = {"schema_version", "name", "media", "composition", "timelines", "transcripts", "censor", "metadata", "subtitles", "highlights"}
        extra = {k: v for k, v in data.items() if k not in known}
        composition = None
        if data.get("composition") is not None:
            try:
                composition = Composition.from_dict(data["composition"])
            except (TypeError, ValueError) as exc:
                raise ProjectFormatError(f"Invalid composition: {exc}") from exc
        timelines: dict[str, Timeline] = {}
        for entry in data.get("timelines") or []:
            try:
                timelines[str(entry["media"])] = Timeline.from_dict(entry)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ProjectFormatError(f"Invalid timeline: {exc}") from exc
        transcripts: dict[str, Transcript] = {}
        for entry in data.get("transcripts") or []:
            try:
                transcripts[str(entry["media"])] = Transcript.from_dict(entry)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ProjectFormatError(f"Invalid transcript: {exc}") from exc
        censors: dict[str, CensorState] = {}
        for entry in data.get("censor") or []:
            try:
                censors[str(entry["media"])] = CensorState.from_dict(entry)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ProjectFormatError(f"Invalid censor data: {exc}") from exc
        highlights: dict[str, tuple[Highlight, ...]] = {}
        for entry in data.get("highlights") or []:
            try:
                highlights[str(entry["media"])] = tuple(Highlight.from_dict(h) for h in entry.get("items") or ())
            except (KeyError, TypeError, ValueError):
                continue  # only a search result: drop it rather than refuse the project
        metadata = ClipMetadata()
        if data.get("metadata") is not None:
            try:
                metadata = ClipMetadata.from_dict(data["metadata"])
            except (TypeError, ValueError) as exc:
                raise ProjectFormatError(f"Invalid metadata: {exc}") from exc
        return cls(
            name=str(data.get("name") or "Untitled"),
            media=[MediaRef.from_dict(m) for m in data.get("media") or []],
            composition=composition,
            timelines=timelines,
            transcripts=transcripts,
            censors=censors,
            metadata=metadata,
            subtitles=SubtitleSettings.from_dict(data.get("subtitles")),
            highlights=highlights,
            extra=extra,
        )


def _norm(path: str) -> str:
    return str(Path(path)).casefold()


def save_project(project: Project, path: str | Path) -> Path:
    path = Path(path)
    if path.suffix.lower() != PROJECT_EXTENSION:
        path = path.with_suffix(PROJECT_EXTENSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(project.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def load_project(path: str | Path) -> Project:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ProjectFormatError(f"Not a valid project file: {exc}") from exc
    return Project.from_dict(data)

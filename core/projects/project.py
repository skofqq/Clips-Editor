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

from core.editor.composition import Composition
from core.timeline.model import Timeline

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
        for stored in [p for p in self.timelines if _norm(p) == key]:
            del self.timelines[stored]

    def timeline_for(self, path: str) -> Timeline | None:
        key = _norm(path)
        return next((t for p, t in self.timelines.items() if _norm(p) == key), None)

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
        known = {"schema_version", "name", "media", "composition", "timelines"}
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
        return cls(
            name=str(data.get("name") or "Untitled"),
            media=[MediaRef.from_dict(m) for m in data.get("media") or []],
            composition=composition,
            timelines=timelines,
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

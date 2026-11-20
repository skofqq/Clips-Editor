"""Composition presets: built-in ones plus user presets in a JSON file."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path

from core.editor.composition import GAMEPLAY, WEBCAM, Composition, default_composition

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Preset:
    name: str
    composition: Composition
    builtin: bool = False


def builtin_presets() -> list[Preset]:
    presets = [
        Preset(f"Webcam {corner.replace('-', ' ')}", default_composition(1920, 1080, corner), True)
        for corner in ("top-left", "top-right", "bottom-left", "bottom-right")
    ]
    base = default_composition(1920, 1080)
    gameplay_only = base.with_region(WEBCAM, replace(base.webcam, enabled=False))
    webcam_only = base.with_region(GAMEPLAY, replace(base.gameplay, enabled=False))
    presets.append(Preset("Gameplay only", gameplay_only, True))
    presets.append(Preset("Webcam only", webcam_only, True))
    return presets


class PresetStore:
    """User presets persisted to ``path``; built-ins are always available."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._user: dict[str, Composition] = {}
        self._builtin = builtin_presets()
        self.load()

    def load(self) -> None:
        self._user.clear()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Could not read presets %s: %s", self.path, exc)
            return
        for entry in raw.get("presets", []) if isinstance(raw, dict) else []:
            try:
                name = str(entry["name"]).strip()
                if name:
                    self._user[name] = Composition.from_dict(entry["composition"])
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Skipping invalid preset: %s", exc)

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "presets": [
                {"name": name, "composition": comp.to_dict()} for name, comp in self._user.items()
            ],
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def all(self) -> list[Preset]:
        return self._builtin + [Preset(name, comp) for name, comp in self._user.items()]

    def get(self, name: str) -> Preset | None:
        return next((p for p in self.all() if p.name == name), None)

    def is_builtin(self, name: str) -> bool:
        return any(p.name == name for p in self._builtin)

    def save(self, name: str, composition: Composition) -> None:
        name = name.strip()
        if not name:
            raise ValueError("Preset name is empty.")
        if self.is_builtin(name):
            raise ValueError("Built-in presets cannot be replaced.")
        # Face tracking belongs to one clip; a preset keeps only the framing.
        self._user[name] = composition.without_tracks()
        self._write()

    def delete(self, name: str) -> bool:
        if name not in self._user:
            return False
        del self._user[name]
        self._write()
        return True

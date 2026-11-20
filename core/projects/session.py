"""Autosave (crash recovery copies) and undo/redo history kept between runs.

Two kinds of files live in the app data folder, never next to the user's
sources or project:

* ``recovery/<key>.json`` - the latest unsaved state of an open project. It
  is written every few minutes while there are unsaved changes and removed
  as soon as the project is saved or the user discards the changes. A file
  that is still there on the next start means the app did not close
  normally, so its work can be restored.
* ``history/<hash of the project path>.json`` - the undo/redo steps of a
  saved project, written on every save, so that Ctrl+Z still works after
  the project is opened again.

An edit state is the tuple the main window keeps in its History:
``(composition, timeline, transcript, censor)``. Many steps share the same
transcript or composition, so every part is stored once in a pool and the
steps refer to it by hash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.censor.review import CensorState
from core.editor.composition import Composition
from core.timeline.model import Timeline
from core.transcription.model import Transcript

log = logging.getLogger(__name__)

HISTORY_FORMAT = 1
PERSISTED_STEPS = 60  # undo steps kept on disk per clip (redo steps too)
_PARTS = (Composition, Timeline, Transcript, CensorState)

# ------------------------------------------------------------ edit states

EditState = tuple  # (Composition | None, Timeline | None, Transcript | None, CensorState | None)
HistorySnapshot = tuple  # (undo: list[EditState], current: EditState, redo: list[EditState])


def norm_path(path: str | Path) -> str:
    return str(Path(path)).casefold()


def _part_key(part: Any, pool: dict[str, Any], cache: dict[int, str]) -> str | None:
    if part is None:
        return None
    cached = cache.get(id(part))
    if cached is not None:
        return cached
    data = part.to_dict()
    text = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    pool.setdefault(key, data)
    cache[id(part)] = key
    return key


def encode_histories(histories: dict[str, HistorySnapshot], steps: int = PERSISTED_STEPS) -> dict[str, Any]:
    """Turn ``{media path: (undo, current, redo)}`` into a JSON-ready dict."""
    pool: dict[str, Any] = {}
    cache: dict[int, str] = {}  # id(part) -> key; parts are immutable and alive during the call

    def encode(state: EditState) -> list[str | None]:
        return [_part_key(part, pool, cache) for part in state]

    clips = []
    for media, (undo, current, redo) in histories.items():
        clips.append({
            "media": media,
            "undo": [encode(s) for s in undo[-steps:]],
            "current": encode(current),
            "redo": [encode(s) for s in redo[-steps:]],
        })
    return {"format": HISTORY_FORMAT, "pool": pool, "clips": clips}


def decode_histories(data: Any) -> dict[str, HistorySnapshot]:
    """Inverse of :func:`encode_histories`. Broken entries are skipped."""
    if not isinstance(data, dict) or data.get("format") != HISTORY_FORMAT:
        return {}
    raw_pool = data.get("pool") if isinstance(data.get("pool"), dict) else {}
    decoded: dict[tuple[int, str], Any] = {}

    def part(index: int, key: Any) -> Any:
        if key is None:
            return None
        if (index, key) not in decoded:
            decoded[(index, key)] = _PARTS[index].from_dict(raw_pool[key])
        return decoded[(index, key)]

    def state(keys: Any) -> EditState:
        if not isinstance(keys, list) or len(keys) != len(_PARTS):
            raise ValueError("Bad history step.")
        return tuple(part(i, k) for i, k in enumerate(keys))

    result: dict[str, HistorySnapshot] = {}
    for clip in data.get("clips") or []:
        try:
            media = str(clip["media"])
            result[media] = (
                [state(s) for s in clip.get("undo") or []],
                state(clip["current"]),
                [state(s) for s in clip.get("redo") or []],
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("Skipping a broken undo history entry: %s", exc)
    return result


def _write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return path


# ---------------------------------------------------- history of saved projects

def history_file(data_dir: Path, project_path: str | Path) -> Path:
    digest = hashlib.sha1(norm_path(Path(project_path).resolve()).encode("utf-8")).hexdigest()[:20]
    return data_dir / "history" / f"{digest}.json"


def save_history(data_dir: Path, project_path: str | Path, histories: dict[str, HistorySnapshot]) -> Path:
    """Write the undo history of a saved project (safe to call from a worker)."""
    data = encode_histories(histories)
    data["project"] = str(project_path)
    data["saved_at"] = time.time()
    return _write_json(history_file(data_dir, project_path), data)


def load_history(data_dir: Path, project_path: str | Path) -> dict[str, HistorySnapshot]:
    path = history_file(data_dir, project_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Cannot read undo history %s: %s", path, exc)
        return {}
    return decode_histories(data)


# ------------------------------------------------------------ recovery copies

@dataclass(frozen=True)
class RecoveryEntry:
    key: str
    path: Path
    saved_at: float
    project_name: str
    original_path: str  # "" for a project that was never saved
    media_count: int


class RecoveryStore:
    """Crash-recovery copies of open projects in ``<data>/recovery``."""

    def __init__(self, data_dir: Path) -> None:
        self.folder = data_dir / "recovery"

    @staticmethod
    def key_for(project_path: str | Path | None, session: str) -> str:
        if project_path:
            return "p-" + hashlib.sha1(norm_path(Path(project_path).resolve()).encode("utf-8")).hexdigest()[:20]
        return "u-" + session

    @staticmethod
    def new_session() -> str:
        return uuid.uuid4().hex[:12]

    def file_for(self, key: str) -> Path:
        return self.folder / f"{key}.json"

    def write(self, key: str, project: dict[str, Any], histories: dict[str, HistorySnapshot],
              original_path: str | Path | None) -> Path:
        """Store a recovery copy (safe to call from a worker thread)."""
        return _write_json(self.file_for(key), {
            "format": HISTORY_FORMAT,
            "saved_at": time.time(),
            "original_path": str(original_path or ""),
            "project": project,
            "history": encode_histories(histories),
        })

    def discard(self, key: str | None) -> None:
        if not key:
            return
        try:
            self.file_for(key).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Cannot remove recovery copy %s: %s", key, exc)

    def entries(self) -> list[RecoveryEntry]:
        """Recovery copies, newest first."""
        result = []
        for path in self.folder.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                project = data["project"]
                result.append(RecoveryEntry(
                    key=path.stem,
                    path=path,
                    saved_at=float(data.get("saved_at") or path.stat().st_mtime),
                    project_name=str(project.get("name") or "Untitled"),
                    original_path=str(data.get("original_path") or ""),
                    media_count=len(project.get("media") or []),
                ))
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                log.warning("Ignoring unreadable recovery copy %s: %s", path, exc)
        result.sort(key=lambda e: e.saved_at, reverse=True)
        return result

    def load(self, entry: RecoveryEntry) -> tuple[dict[str, Any], dict[str, HistorySnapshot]]:
        """(project dict, histories) of a recovery copy."""
        data = json.loads(entry.path.read_text(encoding="utf-8"))
        return data["project"], decode_histories(data.get("history"))

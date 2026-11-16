"""Application configuration: paths and user settings stored as JSON."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "Twitch Shorts Studio"
APP_DIR_NAME = "TwitchShortsStudio"
APP_VERSION = "0.1.0"

log = logging.getLogger(__name__)


def project_root() -> Path:
    """Folder that contains the application code (or the packaged exe)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """Per-user folder for settings, logs and cache."""
    override = os.environ.get("TSS_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


@dataclass
class AppConfig:
    """User settings. Unknown keys in the file are ignored on load."""

    ffmpeg_dir: str = ""  # optional folder that contains ffmpeg/ffprobe
    last_import_dir: str = ""
    recent_files: list[str] = field(default_factory=list)
    window_geometry: str = ""  # base64 of QMainWindow.saveGeometry()
    window_state: str = ""  # base64 of QMainWindow.saveState()

    data_dir: Path = field(default_factory=user_data_dir, compare=False)

    @property
    def path(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def add_recent(self, file_path: str, limit: int = 15) -> None:
        if file_path in self.recent_files:
            self.recent_files.remove(file_path)
        self.recent_files.insert(0, file_path)
        del self.recent_files[limit:]

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("data_dir")
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "AppConfig":
        config = cls(data_dir=data_dir or user_data_dir())
        try:
            raw = json.loads(config.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return config
        except (OSError, ValueError) as exc:
            log.warning("Could not read config %s: %s. Using defaults.", config.path, exc)
            return config
        if not isinstance(raw, dict):
            return config
        for key in ("ffmpeg_dir", "last_import_dir", "window_geometry", "window_state"):
            if isinstance(raw.get(key), str):
                setattr(config, key, raw[key])
        recent = raw.get("recent_files")
        if isinstance(recent, list):
            config.recent_files = [item for item in recent if isinstance(item, str)]
        return config

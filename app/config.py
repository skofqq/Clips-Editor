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
APP_VERSION = "0.8.7"

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
    timeline_view: dict = field(default_factory=dict)  # track heights and waveform zoom
    theme: str = "dark"  # dark | light | system (follows Windows)
    mica: bool = False  # Windows 11 Mica backdrop behind the command bar
    transcribe_vocabulary: str = ""  # nicknames / slang, comma separated
    transcribe_uncensored: bool = True
    transcribe_slang: bool = True
    transcribe_clean_audio: bool = True
    transcribe_vad: str = "normal"  # normal | sensitive | off
    transcribe_accurate: bool = False
    transcribe_custom_model: str = ""  # folder or Hugging Face id
    transcribe_learned: list[str] = field(default_factory=list)  # words from the user's corrections
    transcribe_model: str = ""
    transcribe_language: str = ""
    export_dir: str = ""  # last folder a video was exported to
    export_settings: dict = field(default_factory=dict)  # RenderSettings.to_dict()
    youtube_accounts: list[dict] = field(default_factory=list)  # Channel.to_dict(); tokens are NOT here
    youtube_active: str = ""  # channel id
    youtube_defaults: dict = field(default_factory=dict)  # last category / visibility / language / tags

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
        for key in (
            "ffmpeg_dir", "last_import_dir", "window_geometry", "window_state",
            "transcribe_vocabulary", "transcribe_model", "transcribe_language", "export_dir",
            "youtube_active", "transcribe_vad", "transcribe_custom_model",
        ):
            if isinstance(raw.get(key), str):
                setattr(config, key, raw[key])
        if isinstance(raw.get("transcribe_uncensored"), bool):
            config.transcribe_uncensored = raw["transcribe_uncensored"]
        if raw.get("theme") in ("dark", "light", "system"):
            config.theme = raw["theme"]
        for key in ("transcribe_slang", "transcribe_clean_audio", "transcribe_accurate", "mica"):
            if isinstance(raw.get(key), bool):
                setattr(config, key, raw[key])
        learned = raw.get("transcribe_learned")
        if isinstance(learned, list):
            config.transcribe_learned = [w for w in learned if isinstance(w, str)][:100]
        for key in ("export_settings", "youtube_defaults", "timeline_view"):
            if isinstance(raw.get(key), dict):
                setattr(config, key, raw[key])
        accounts = raw.get("youtube_accounts")
        if isinstance(accounts, list):
            config.youtube_accounts = [a for a in accounts if isinstance(a, dict) and a.get("id")]
        recent = raw.get("recent_files")
        if isinstance(recent, list):
            config.recent_files = [item for item in recent if isinstance(item, str)]
        return config

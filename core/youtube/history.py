"""Record of finished uploads (video IDs and URLs), in the user data folder."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UploadRecord:
    uploaded_at: str  # ISO time, local
    file: str
    video_id: str
    url: str
    title: str
    channel_id: str
    channel_title: str
    privacy: str
    publish_at: str = ""


class UploadHistory:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def records(self) -> list[UploadRecord]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("Could not read upload history: %s", exc)
            return []
        items = raw.get("uploads") if isinstance(raw, dict) else None
        result = []
        for item in items or []:
            try:
                result.append(UploadRecord(**{k: str(item.get(k, "")) for k in UploadRecord.__dataclass_fields__}))
            except (TypeError, AttributeError):
                continue
        return result

    def add(self, record: UploadRecord) -> None:
        records = self.records() + [record]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps({"version": 1, "uploads": [asdict(r) for r in records]}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def for_file(self, file: str | Path) -> list[UploadRecord]:
        key = str(Path(file)).casefold()
        return [r for r in self.records() if str(Path(r.file)).casefold() == key]

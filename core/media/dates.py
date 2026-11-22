"""Dates for the Media list's groups (month → day).

A clip's date is, by default, the date written in its file name
("2026-11-20 streamer – clip.mp4", "clip_20261120.mp4", "20.11.2026 …"),
which is usually the stream date, and otherwise the file's modification time
(Windows keeps it when a file is copied, unlike the creation time). The other
choice is the day the clip was first imported, remembered per file.

``MediaGroupsStore`` keeps the panel's settings in a small JSON file next to
the app's config: grouping, date source, collapsed groups and import times.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

GROUP_MONTH_DAY = "month_day"
GROUP_MONTH = "month"
GROUP_DAY = "day"
GROUP_NONE = "none"
GROUPINGS = (GROUP_MONTH_DAY, GROUP_MONTH, GROUP_DAY, GROUP_NONE)

SOURCE_CLIP = "clip"      # date in the name, else the file's modification time
SOURCE_IMPORT = "import"  # the day the clip was first imported
SOURCES = (SOURCE_CLIP, SOURCE_IMPORT)

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Year first (2026-11-20, 2026_11_20, 2026.11.20, 20261120) or day first (20.11.2026).
_YMD = re.compile(r"(?<!\d)(20\d\d)([-_.]?)(0[1-9]|1[0-2])\2(0[1-9]|[12]\d|3[01])(?!\d)")
_DMY = re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])([-_.])(0[1-9]|1[0-2])\2(20\d\d)(?!\d)")


def date_from_name(name: str) -> date | None:
    """The first valid calendar date written in a file name, or None."""
    stem = Path(name).stem
    found: list[tuple[int, date]] = []
    for match in _YMD.finditer(stem):
        try:
            found.append((match.start(), date(int(match[1]), int(match[3]), int(match[4]))))
        except ValueError:
            pass
    for match in _DMY.finditer(stem):
        try:
            found.append((match.start(), date(int(match[4]), int(match[3]), int(match[1]))))
        except ValueError:
            pass
    if not found:
        return None
    day = min(found, key=lambda pair: pair[0])[1]
    return day if day <= date.today() else None  # a "date" in the future is some other number


def file_time(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime)
    except (OSError, ValueError, OverflowError):
        return None


def path_key(path: Path | str) -> str:
    return str(path).casefold()


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def day_key(day: date) -> str:
    return day.isoformat()


def month_label(key: str, today: date | None = None) -> str:
    """"2026-11" → "November 2026" ("November" alone in the current year)."""
    year, month = (int(part) for part in key.split("-"))
    today = today or date.today()
    name = MONTHS[month - 1]
    return name if year == today.year else f"{name} {year}"


def day_label(key: str, today: date | None = None) -> str:
    """"2026-11-22" → "Today", "Yesterday" or "Fri, 20 Nov" (with the year when not this year)."""
    day = date.fromisoformat(key)
    today = today or date.today()
    delta = (today - day).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    text = f"{_WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1][:3]}"
    return text if day.year == today.year else f"{text} {day.year}"


@dataclass
class MediaGroupsStore:
    """The Media list's grouping settings, saved to ``path`` (None: memory only)."""

    path: Path | None = None
    grouping: str = GROUP_MONTH_DAY
    source: str = SOURCE_CLIP
    collapsed: set[str] = field(default_factory=set)
    imported: dict[str, str] = field(default_factory=dict)  # path key → ISO time of first import

    @classmethod
    def load(cls, path: Path) -> "MediaGroupsStore":
        store = cls(path=Path(path))
        try:
            data = json.loads(store.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return store
        except (OSError, ValueError) as exc:
            log.warning("Media groups file unreadable (%s); starting fresh", exc)
            return store
        if not isinstance(data, dict):
            return store
        if data.get("grouping") in GROUPINGS:
            store.grouping = data["grouping"]
        if data.get("source") in SOURCES:
            store.source = data["source"]
        store.collapsed = {str(k) for k in data.get("collapsed", []) if isinstance(k, str)}
        imported = data.get("imported", {})
        if isinstance(imported, dict):
            store.imported = {str(k): str(v) for k, v in imported.items()}
        return store

    def save(self) -> None:
        if self.path is None:
            return
        data = {
            "grouping": self.grouping,
            "source": self.source,
            "collapsed": sorted(self.collapsed),
            "imported": self.imported,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("Could not save media groups: %s", exc)

    def note_import(self, path: Path, now: datetime | None = None) -> bool:
        """Remember when ``path`` was first imported; True when it is new."""
        key = path_key(path)
        if key in self.imported:
            return False
        self.imported[key] = (now or datetime.now()).isoformat(timespec="seconds")
        return True

    def import_time(self, path: Path) -> datetime | None:
        value = self.imported.get(path_key(path))
        try:
            return datetime.fromisoformat(value) if value else None
        except ValueError:
            return None

    def clip_time(self, path: Path) -> datetime:
        """When the clip belongs, by the chosen source (falls back to now)."""
        path = Path(path)
        imported = self.import_time(path)
        if self.source == SOURCE_IMPORT and imported is not None:
            return imported
        modified = file_time(path)
        named = date_from_name(path.name)
        if named is not None:
            # Same day as the file: keep its time of day for sorting; else midday.
            if modified is not None and modified.date() == named:
                return modified
            return datetime.combine(named, datetime.min.time()).replace(hour=12)
        return modified or imported or datetime.now()

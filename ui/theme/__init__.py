"""Theme assets: the stylesheet (dark.qss) and line icons (icons/*.svg)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtGui import QIcon

ICON_DIR = Path(__file__).resolve().parent / "icons"


@lru_cache(maxsize=None)
def icon(name: str) -> QIcon:
    """``icon("save")`` → QIcon from icons/save.svg (empty icon if missing)."""
    path = ICON_DIR / f"{name}.svg"
    return QIcon(str(path)) if path.is_file() else QIcon()

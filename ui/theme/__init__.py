"""Theme assets: the Fluent stylesheet (dark.qss), palette tokens and line icons.

Icons in ``icons/`` use ``currentColor`` so one file serves every tint.
Glyphs the stylesheet needs (chevrons, check marks) are written to a small
cache folder with the current accent colours, because Qt style sheets can
only load images from files.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap

from ui.theme import palette

log = logging.getLogger(__name__)

THEME_DIR = Path(__file__).resolve().parent
ICON_DIR = THEME_DIR / "icons"
STYLESHEET = THEME_DIR / "dark.qss"
ICON_SIZES = (16, 20, 24, 32, 40, 48)


def _svg(name: str, color: str) -> bytes | None:
    path = ICON_DIR / f"{name}.svg"
    try:
        return path.read_text(encoding="utf-8").replace("currentColor", color).encode("utf-8")
    except OSError:
        return None


def _render(data: bytes, size: int) -> QPixmap:
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(QByteArray(data))
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return QPixmap.fromImage(image)


def icon(name: str, color: str | None = None, disabled: str | None = None) -> QIcon:
    """``icon("save")`` → QIcon from icons/save.svg tinted ``color`` (default: the
    theme's text colour; empty if missing)."""
    return _icon(name, color or palette.TEXT, disabled or palette.TEXT_DISABLED)


@lru_cache(maxsize=None)
def _icon(name: str, color: str, disabled: str) -> QIcon:
    data = _svg(name, color)
    if data is None:
        return QIcon()
    result = QIcon()
    off = _svg(name, disabled)
    for size in ICON_SIZES:
        result.addPixmap(_render(data, size), QIcon.Mode.Normal)
        result.addPixmap(_render(data, size), QIcon.Mode.Active)
        if off is not None:
            result.addPixmap(_render(off, size), QIcon.Mode.Disabled)
    return result


# --------------------------------------------------------------- stylesheet
_GLYPHS = {
    "chevron-down": '<path d="M4 6.25l4 4 4-4" fill="none" stroke="{c}" stroke-width="1.3" '
                    'stroke-linecap="round" stroke-linejoin="round"/>',
    "chevron-up": '<path d="M4 9.75l4-4 4 4" fill="none" stroke="{c}" stroke-width="1.3" '
                  'stroke-linecap="round" stroke-linejoin="round"/>',
    "chevron-down-disabled": '<path d="M4 6.25l4 4 4-4" fill="none" stroke="{d}" stroke-width="1.3" '
                             'stroke-linecap="round" stroke-linejoin="round"/>',
    "check": '<path d="M3.5 8.4l2.9 2.9 6.1-6.6" fill="none" stroke="{a}" stroke-width="1.5" '
             'stroke-linecap="round" stroke-linejoin="round"/>',
    "dot": '<circle cx="8" cy="8" r="3.6" fill="{a}"/>',
}


def glyph_dir() -> Path:
    """Folder with the stylesheet glyphs for the current accent (created on demand)."""
    accent = palette.accent()
    key = hashlib.sha1(f"{accent}|{palette.TEXT_SECONDARY}".encode()).hexdigest()[:10]
    folder = Path(tempfile.gettempdir()) / f"tss-theme-{key}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name, body in _GLYPHS.items():
            path = folder / f"{name}.svg"
            if not path.exists():
                svg = body.format(c=palette.TEXT_SECONDARY, d=palette.TEXT_DISABLED, a=accent.on_fill)
                path.write_text(
                    f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">{svg}</svg>',
                    encoding="utf-8",
                )
    except OSError as exc:
        log.warning("Theme glyphs not written: %s", exc)
    return folder


def stylesheet() -> str:
    try:
        text = STYLESHEET.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Theme not loaded: %s", exc)
        return ""
    text = palette.theme_qss(text)
    for token, value in palette.tokens().items():
        text = text.replace(token, value)
    # Qt wants forward slashes in url(); the folder may contain spaces, so urls are quoted.
    return text.replace("@GLYPHS@", glyph_dir().as_posix())

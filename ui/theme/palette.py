"""Fluent Design (Windows 11) dark palette and the user's accent colour.

The tokens follow WinUI's dark theme resources (solid equivalents of the
semi-transparent fills over the Mica base). The accent comes from Windows
(Settings → Personalization → Colors): dark mode uses the "Light 2" shade
of the accent palette, as WinUI does. Elsewhere, or if the registry cannot
be read, the default Windows blue is used. ``TSS_ACCENT=#rrggbb`` overrides it.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- neutrals
BASE = "#202020"            # SolidBackgroundFillColorBase (window, Mica fallback)
BASE_ALT = "#1c1c1c"        # SolidBackgroundFillColorSecondary
LAYER = "#272727"           # LayerFillColorDefault over the base: panels / cards
LAYER_ALT = "#2b2b2b"       # CardBackgroundFillColorDefault
CONTROL = "#2d2d2d"         # ControlFillColorDefault
CONTROL_HOVER = "#323232"   # ControlFillColorSecondary
CONTROL_PRESSED = "#272727"  # ControlFillColorTertiary
CONTROL_DISABLED = "#262626"
INPUT_FOCUSED = "#1f1f1f"   # TextControlBackgroundFocused
SUBTLE_HOVER = "#2a2a2a"    # SubtleFillColorSecondary over the base
SUBTLE_PRESSED = "#262626"
STROKE = "#363636"          # ControlStrokeColorDefault
STROKE_STRONG = "#8a8a8a"   # ControlStrongStrokeColorDefault (unchecked boxes, input underline)
DIVIDER = "#2f2f2f"         # DividerStrokeColorDefault
CARD_STROKE = "#1d1d1d"     # CardStrokeColorDefault (darker edge)
FLYOUT = "#2c2c2c"          # AcrylicInAppFillColorDefault (menus, popups)
FLYOUT_STROKE = "#3b3b3b"

TEXT = "#ffffff"
TEXT_SECONDARY = "#c5c5c5"
TEXT_TERTIARY = "#9d9d9d"
TEXT_DISABLED = "#5d5d5d"

CRITICAL = "#ff99a4"        # SystemFillColorCritical (dark)
CAUTION = "#fce100"
SUCCESS = "#6ccb5f"

DEFAULT_ACCENT = "#60cdff"  # Windows default blue, "Light 2" shade used in dark mode
DEFAULT_ACCENT_BASE = "#0078d4"


def _hex(r: int, g: int, b: int) -> str:
    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def mix(a: str, b: str, amount: float) -> str:
    """``amount`` of ``b`` blended into ``a``."""
    (r1, g1, b1), (r2, g2, b2) = _rgb(a), _rgb(b)
    return _hex(*(round(x + (y - x) * amount) for x, y in ((r1, r2), (g1, g2), (b1, b2))))


def luminance(color: str) -> float:
    def channel(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _windows_accent() -> str | None:
    """Light 2 shade of the Windows accent palette, if it can be read."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent") as key:
            palette, _ = winreg.QueryValueEx(key, "AccentPalette")
        if isinstance(palette, (bytes, bytearray)) and len(palette) >= 8:
            r, g, b = palette[4], palette[5], palette[6]  # entry 1 = Light 2 (RGBA entries)
            return _hex(r, g, b)
    except OSError as exc:
        log.debug("No accent palette: %s", exc)
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as key:
            value, _ = winreg.QueryValueEx(key, "AccentColor")  # 0xAABBGGRR
        r, g, b = value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
        return mix(_hex(r, g, b), "#ffffff", 0.45)
    except OSError as exc:
        log.debug("No accent colour: %s", exc)
    return None


@dataclass(frozen=True)
class Accent:
    fill: str          # accent buttons, checked boxes, slider, selection indicator
    hover: str
    pressed: str
    on_fill: str       # text / glyphs on the accent fill
    text: str          # accent used as text or lines on the dark base
    soft: str          # faint accent tint for selections on canvases

    def tokens(self) -> dict[str, str]:
        return {
            "@ACCENT@": self.fill, "@ACCENT_HOVER@": self.hover, "@ACCENT_PRESSED@": self.pressed,
            "@ON_ACCENT@": self.on_fill, "@ACCENT_TEXT@": self.text, "@ACCENT_SOFT@": self.soft,
        }


def make_accent(fill: str) -> Accent:
    dark_text = luminance(fill) > 0.35
    return Accent(
        fill=fill,
        hover=mix(fill, BASE, 0.10),     # WinUI: AccentFillColorSecondary (90 % opacity)
        pressed=mix(fill, BASE, 0.20),   # AccentFillColorTertiary (80 %)
        on_fill="#000000" if dark_text else "#ffffff",
        text=fill if luminance(fill) > 0.18 else mix(fill, "#ffffff", 0.45),
        soft=mix(BASE, fill, 0.22),
    )


@lru_cache(maxsize=1)
def accent() -> Accent:
    override = os.environ.get("TSS_ACCENT", "").strip()
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", override):
        return make_accent("#" + override.lstrip("#").lower())
    return make_accent(_windows_accent() or DEFAULT_ACCENT)


def tokens() -> dict[str, str]:
    """Placeholders used in the stylesheet."""
    return accent().tokens()

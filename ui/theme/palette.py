"""Fluent Design (Windows 11) palette, light or dark, and the user's accent colour.

The constants below are the dark values. :func:`set_theme` switches the
module to the light theme (WinUI light resources) before the UI is built;
:func:`themed` and :func:`theme_qss` translate any dark colour used in a
stylesheet or in painting code to its light counterpart.

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
DEFAULT_ACCENT_LIGHT = "#005fb8"  # "Dark 1" shade used in light mode

DARK, LIGHT, SYSTEM = "dark", "light", "system"
THEMES = (SYSTEM, DARK, LIGHT)

# Every dark colour used by the stylesheet or painting code → its light twin.
_LIGHT = {
    "#202020": "#f3f3f3", "#1c1c1c": "#eeeeee", "#1d1d1d": "#e5e5e5", "#1f1f1f": "#ffffff",
    "#222222": "#f6f6f6", "#252525": "#fbfbfb", "#262626": "#f5f5f5", "#272727": "#f9f9f9",
    "#2a2a2a": "#e8e8e8", "#2b2b2b": "#fbfbfb", "#2c2c2c": "#fcfcfc", "#2d2d2d": "#fdfdfd",
    "#2f2f2f": "#e1e1e1", "#303030": "#e0e0e0", "#323232": "#f6f6f6", "#333333": "#ededed",
    "#363636": "#e5e5e5", "#383838": "#e8e8e8", "#3a3a3a": "#e6e6e6", "#3b3b3b": "#dedede",
    "#3d3d3d": "#d6d6d6", "#414141": "#dcdcdc", "#444444": "#cfcfcf", "#454545": "#ffffff",
    "#5d5d5d": "#a3a3a3", "#6a6a6a": "#a8a8a8", "#8a8a8a": "#868686", "#9a9a9a": "#b0b0b0",
    "#9d9d9d": "#707070", "#c5c5c5": "#5d5d5d", "#ffffff": "#1b1b1b",
    "#ff99a4": "#c42b1c", "#fce100": "#9d5d00", "#6ccb5f": "#0f7b0f",
    "#2f5a3f": "#cfe8d6", "#dfffee": "#0d3b1e",
}
_NAMES = (
    "BASE", "BASE_ALT", "LAYER", "LAYER_ALT", "CONTROL", "CONTROL_HOVER", "CONTROL_PRESSED",
    "CONTROL_DISABLED", "INPUT_FOCUSED", "SUBTLE_HOVER", "SUBTLE_PRESSED", "STROKE", "STROKE_STRONG",
    "DIVIDER", "CARD_STROKE", "FLYOUT", "FLYOUT_STROKE", "TEXT", "TEXT_SECONDARY", "TEXT_TERTIARY",
    "TEXT_DISABLED", "CRITICAL", "CAUTION", "SUCCESS",
)
_DARK_VALUES = {name: globals()[name] for name in _NAMES}
_current = DARK
_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")


def windows_prefers_light() -> bool:
    """Windows "app mode" (Settings → Personalization → Colors)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(value)
    except OSError:
        return False


def resolve_theme(name: str) -> str:
    name = (name or SYSTEM).lower()
    if name == SYSTEM:
        return LIGHT if windows_prefers_light() else DARK
    return LIGHT if name == LIGHT else DARK


def set_theme(name: str) -> str:
    """Switch the module constants to ``name`` (dark / light / system). Call
    before the UI is created: widgets read the colours when they are built."""
    global _current
    _current = resolve_theme(name)
    for key, value in _DARK_VALUES.items():
        globals()[key] = themed(value)
    accent.cache_clear()
    return _current


def current_theme() -> str:
    return _current


def is_light() -> bool:
    return _current == LIGHT


def themed(color: str) -> str:
    """The colour to use for dark-theme ``color`` in the current theme."""
    if _current != LIGHT:
        return color
    return _LIGHT.get(color.lower(), color)


def theme_qss(text: str) -> str:
    """Translate every dark colour in a stylesheet to the current theme."""
    if _current != LIGHT:
        return text
    return _HEX.sub(lambda m: _LIGHT.get(m.group(0).lower(), m.group(0)), text)


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


def _windows_accent(light: bool = False) -> str | None:
    """Light 2 (dark theme) or Dark 1 (light theme) shade of the Windows accent."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent") as key:
            palette, _ = winreg.QueryValueEx(key, "AccentPalette")
        if isinstance(palette, (bytes, bytearray)) and len(palette) >= 8:
            i = 16 if light else 4  # RGBA entries: Light3, Light2, Light1, Base, Dark1, ...
            if len(palette) >= i + 3:
                return _hex(palette[i], palette[i + 1], palette[i + 2])
    except OSError as exc:
        log.debug("No accent palette: %s", exc)
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as key:
            value, _ = winreg.QueryValueEx(key, "AccentColor")  # 0xAABBGGRR
        r, g, b = value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
        return mix(_hex(r, g, b), "#000000", 0.25) if light else mix(_hex(r, g, b), "#ffffff", 0.45)
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
    if is_light():
        text = fill if luminance(fill) < 0.25 else mix(fill, "#000000", 0.35)
    else:
        text = fill if luminance(fill) > 0.18 else mix(fill, "#ffffff", 0.45)
    return Accent(
        fill=fill,
        hover=mix(fill, BASE, 0.10),     # WinUI: AccentFillColorSecondary (90 % opacity)
        pressed=mix(fill, BASE, 0.20),   # AccentFillColorTertiary (80 %)
        on_fill="#000000" if dark_text else "#ffffff",
        text=text,
        soft=mix(BASE, fill, 0.22 if not is_light() else 0.16),
    )


@lru_cache(maxsize=1)
def accent() -> Accent:
    override = os.environ.get("TSS_ACCENT", "").strip()
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", override):
        return make_accent("#" + override.lstrip("#").lower())
    if is_light():
        return make_accent(_windows_accent(light=True) or DEFAULT_ACCENT_LIGHT)
    return make_accent(_windows_accent() or DEFAULT_ACCENT)


def tokens() -> dict[str, str]:
    """Placeholders used in the stylesheet."""
    return accent().tokens()

"""Twitch-coloured Material 3 Expressive palette, dark or light.

The app wears Twitch's brand colours so it feels at home next to the
stream: the purple #9146FF as the primary colour and Twitch's near-black
(dark) or soft-grey (light) surfaces. Shapes, buttons and states follow
Material 3 Expressive in compact (desktop) density; see ``m3.qss``.

The module constants hold the current theme's colours. :func:`set_theme`
switches them before the UI is built; :func:`themed` and :func:`theme_qss`
translate the older hard-coded dark greys still used in a few painting
spots and small stylesheets. ``TSS_ACCENT=#rrggbb`` overrides the purple.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache

log = logging.getLogger(__name__)

TWITCH_PURPLE = "#9146ff"
DEFAULT_ACCENT = TWITCH_PURPLE

# Role → colour, dark (Twitch dark mode) and light (Twitch light mode).
_ROLES = {
    "dark": {
        "BASE": "#0e0e10",            # window background
        "BASE_ALT": "#09090b",        # recessed areas: timeline, page bar
        "LAYER": "#18181b",           # panels, cards
        "LAYER_ALT": "#1f1f23",       # raised cards, dialogs
        "CONTROL": "#2e2e35",         # tonal buttons, chips
        "CONTROL_HOVER": "#3a3a42",
        "CONTROL_PRESSED": "#26262c",
        "CONTROL_DISABLED": "#1f1f23",
        "INPUT": "#1f1f23",           # text fields
        "INPUT_FOCUSED": "#0e0e10",
        "SUBTLE_HOVER": "#26262c",    # state layer on surfaces
        "SUBTLE_PRESSED": "#1f1f23",
        "STROKE": "#35353b",          # outline-variant
        "STROKE_STRONG": "#6b6b78",   # outline: unchecked boxes, field borders
        "DIVIDER": "#26262c",
        "CARD_STROKE": "#232328",
        "FLYOUT": "#1f1f23",          # menus, popups
        "FLYOUT_STROKE": "#35353b",
        "TEXT": "#efeff1",
        "TEXT_SECONDARY": "#adadb8",
        "TEXT_TERTIARY": "#848494",
        "TEXT_DISABLED": "#53535f",
        "INVERSE": "#efeff1",         # tooltips
        "ON_INVERSE": "#0e0e10",
        "CRITICAL": "#ff8280",
        "CAUTION": "#ffca5f",
        "SUCCESS": "#00f593",
    },
    "light": {
        "BASE": "#f7f7f8",
        "BASE_ALT": "#efeff1",
        "LAYER": "#ffffff",
        "LAYER_ALT": "#ffffff",
        "CONTROL": "#e6e6ea",
        "CONTROL_HOVER": "#dcdce1",
        "CONTROL_PRESSED": "#d2d2d8",
        "CONTROL_DISABLED": "#efeff1",
        "INPUT": "#f2f2f4",
        "INPUT_FOCUSED": "#ffffff",
        "SUBTLE_HOVER": "#ececef",
        "SUBTLE_PRESSED": "#e3e3e7",
        "STROKE": "#dedee3",
        "STROKE_STRONG": "#9a9aa6",
        "DIVIDER": "#e6e6ea",
        "CARD_STROKE": "#e6e6ea",
        "FLYOUT": "#ffffff",
        "FLYOUT_STROKE": "#dedee3",
        "TEXT": "#0e0e10",
        "TEXT_SECONDARY": "#53535f",
        "TEXT_TERTIARY": "#6e6e7a",
        "TEXT_DISABLED": "#aeaeb8",
        "INVERSE": "#18181b",
        "ON_INVERSE": "#efeff1",
        "CRITICAL": "#d20000",
        "CAUTION": "#9d5d00",
        "SUCCESS": "#00824f",
    },
}
_NAMES = tuple(_ROLES["dark"])
globals().update(_ROLES["dark"])

DARK, LIGHT, SYSTEM = "dark", "light", "system"
THEMES = (SYSTEM, DARK, LIGHT)

# Older hard-coded dark greys (Fluent era) → the role that replaced them.
_LEGACY = {
    "#202020": "BASE", "#1c1c1c": "BASE_ALT", "#1d1d1d": "CARD_STROKE", "#1f1f1f": "INPUT_FOCUSED",
    "#222222": "LAYER", "#252525": "LAYER", "#262626": "CONTROL_DISABLED", "#272727": "LAYER",
    "#2a2a2a": "SUBTLE_HOVER", "#2b2b2b": "LAYER_ALT", "#2c2c2c": "FLYOUT", "#2d2d2d": "CONTROL",
    "#2f2f2f": "DIVIDER", "#303030": "STROKE", "#323232": "CONTROL_HOVER", "#333333": "STROKE",
    "#363636": "STROKE", "#383838": "CONTROL_HOVER", "#3a3a3a": "STROKE", "#3b3b3b": "FLYOUT_STROKE",
    "#3d3d3d": "STROKE", "#414141": "CONTROL_HOVER", "#444444": "STROKE", "#454545": "CONTROL_HOVER",
    "#5d5d5d": "TEXT_DISABLED", "#6a6a6a": "STROKE_STRONG", "#8a8a8a": "STROKE_STRONG",
    "#9a9a9a": "TEXT_TERTIARY", "#9d9d9d": "TEXT_TERTIARY", "#c5c5c5": "TEXT_SECONDARY",
    "#ffffff": "TEXT", "#ff99a4": "CRITICAL", "#fce100": "CAUTION", "#6ccb5f": "SUCCESS",
}
# Non-role colours that still need a light twin.
_LIGHT_EXTRA = {"#2f5a3f": "#c8f0dc", "#dfffee": "#0d3b1e"}

_current = DARK
_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")


def _theme_map(theme: str) -> dict[str, str]:
    roles = _ROLES[theme]
    result = {old: roles[name] for old, name in _LEGACY.items()}
    if theme == LIGHT:
        result.update({value: roles[name] for name, value in _ROLES[DARK].items() if value not in result})
        result.update(_LIGHT_EXTRA)
    return result


_MAPS = {DARK: _theme_map(DARK), LIGHT: _theme_map(LIGHT)}


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
    globals().update(_ROLES[_current])
    accent.cache_clear()
    return _current


def current_theme() -> str:
    return _current


def is_light() -> bool:
    return _current == LIGHT


def themed(color: str) -> str:
    """The current-theme colour for a dark-theme ``color`` (role colours and
    the older hard-coded greys are translated; anything else is kept)."""
    return _MAPS[_current].get(color.lower(), color)


def theme_qss(text: str) -> str:
    """Translate every known dark colour in a stylesheet to the current theme."""
    table = _MAPS[_current]
    return _HEX.sub(lambda m: table.get(m.group(0).lower(), m.group(0)), text)


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


@dataclass(frozen=True)
class Accent:
    fill: str          # filled buttons, checked boxes, slider, selection indicator
    hover: str
    pressed: str
    on_fill: str       # text / glyphs on the fill
    text: str          # the accent used as text or lines on the surfaces
    soft: str          # faint tint for selections on canvases
    container: str     # M3 primary container: selected chips, active nav item
    on_container: str

    def tokens(self) -> dict[str, str]:
        return {
            "@ACCENT@": self.fill, "@ACCENT_HOVER@": self.hover, "@ACCENT_PRESSED@": self.pressed,
            "@ON_ACCENT@": self.on_fill, "@ACCENT_TEXT@": self.text, "@ACCENT_SOFT@": self.soft,
            "@ACCENT_CONTAINER@": self.container, "@ON_ACCENT_CONTAINER@": self.on_container,
        }


def make_accent(fill: str) -> Accent:
    dark_text = luminance(fill) > 0.35
    on_fill = "#000000" if dark_text else "#ffffff"
    if is_light():
        text = fill if luminance(fill) < 0.12 else mix(fill, "#000000", 0.3)
        container = mix("#ffffff", fill, 0.16)
        on_container = mix(fill, "#000000", 0.55)
    else:
        text = fill if luminance(fill) > 0.18 else mix(fill, "#ffffff", 0.45)
        container = mix(LAYER, fill, 0.32)
        on_container = mix(fill, "#ffffff", 0.75)
    # Like Twitch: a darker shade on hover and press (lighter for pale accents).
    shade = "#ffffff" if dark_text and not is_light() else "#000000"
    return Accent(
        fill=fill,
        hover=mix(fill, shade, 0.16),
        pressed=mix(fill, shade, 0.30),
        on_fill=on_fill,
        text=text,
        soft=mix(BASE, fill, 0.16 if is_light() else 0.24),
        container=container,
        on_container=on_container,
    )


@lru_cache(maxsize=1)
def accent() -> Accent:
    override = os.environ.get("TSS_ACCENT", "").strip()
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", override):
        return make_accent("#" + override.lstrip("#").lower())
    return make_accent(TWITCH_PURPLE)


def tokens() -> dict[str, str]:
    """Placeholders used in the stylesheet: ``@ROLE@`` for every palette role
    plus the accent tokens."""
    result = {f"@{name}@": globals()[name] for name in _NAMES}
    result.update(accent().tokens())
    return result


# One colour per speaker (Find speakers). Mid tones that keep dark word text
# readable in the dark theme and white text in the light theme.
_SPEAKERS_DARK = ("#6ccb5f", "#4cc2ff", "#ffb86b", "#ff8fc8", "#c7a6ff", "#f2e35b")
_SPEAKERS_LIGHT = ("#0f7b0f", "#005fb8", "#b35900", "#bf0077", "#6b46c1", "#8a7400")


def speaker_color(index: int) -> str:
    colors = _SPEAKERS_LIGHT if is_light() else _SPEAKERS_DARK
    return colors[index % len(colors)]

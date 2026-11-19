"""Fluent window chrome: dark title bars, rounded corners, rounded menus.

On Windows 10 1809+ / 11 every top-level window gets the dark title bar
(DWMWA_USE_IMMERSIVE_DARK_MODE) and, on Windows 11, rounded corners plus a
caption colour that matches the window so the title bar blends into the
command bar. Menus and combo-box popups get a translucent background so
their 8px corners from the stylesheet are really round. Everything is best
effort: a failure is logged and the window stays as it is.
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from ui.theme import palette

log = logging.getLogger(__name__)

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19  # Windows 10 before 20H1
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWCP_ROUND, DWMWCP_ROUNDSMALL = 2, 3
DWMWA_SYSTEMBACKDROP_TYPE = 38  # Windows 11 22H2+
DWMSBT_MAINWINDOW = 2  # Mica

_mica = False

# With Mica the window itself is see-through: the command bar, menu bar and
# status bar let the backdrop show; panels and the video stay opaque.
MICA_QSS = """
QMainWindow, QMenuBar, QToolBar#mainToolbar, QStatusBar { background: transparent; }
"""


def mica_enabled() -> bool:
    return _mica


def mica_supported() -> bool:
    """Mica needs Windows 11 build 22621 or later."""
    if sys.platform != "win32":
        return False
    try:
        return sys.getwindowsversion().build >= 22621
    except AttributeError:
        return False


def _colorref(color: str) -> int:
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    return r | (g << 8) | (b << 16)


def apply_window_effects(widget: QWidget, small_corners: bool = False) -> bool:
    """Dark, rounded Windows 11 frame for a top-level widget. Returns True if applied."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        dwm = ctypes.windll.dwmapi
        hwnd = wintypes.HWND(int(widget.winId()))

        def set_attr(attribute: int, value: int) -> int:
            data = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))

        dark = 0 if palette.is_light() else 1
        if set_attr(DWMWA_USE_IMMERSIVE_DARK_MODE, dark) != 0:
            set_attr(DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, dark)
        # The next ones exist on Windows 11 only; older systems just return an error code.
        set_attr(DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUNDSMALL if small_corners else DWMWCP_ROUND)
        mica = _mica and widget.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) and not small_corners
        if mica:
            margins = (ctypes.c_int * 4)(-1, -1, -1, -1)  # sheet of glass: backdrop under the whole client area
            dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
            set_attr(DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_MAINWINDOW)
            set_attr(DWMWA_TEXT_COLOR, _colorref(palette.TEXT))
        elif not small_corners:
            set_attr(DWMWA_CAPTION_COLOR, _colorref(palette.BASE))
            set_attr(DWMWA_TEXT_COLOR, _colorref(palette.TEXT))
            set_attr(DWMWA_BORDER_COLOR, _colorref(palette.FLYOUT_STROKE))
        return True
    except Exception as exc:  # noqa: BLE001 - cosmetic only
        log.debug("Window effects not applied: %s", exc)
        return False


class FluentChrome(QObject):
    """Application-wide event filter that styles windows as they appear."""

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        kind = event.type()
        if kind == QEvent.Type.Polish and isinstance(obj, QWidget):
            if isinstance(obj, QMenu) or type(obj).__name__ == "QComboBoxPrivateContainer":
                obj.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
                obj.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, False)
        elif kind == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            if not obj.property("_fluent_chrome"):
                obj.setProperty("_fluent_chrome", True)
                popup = bool(obj.windowFlags() & Qt.WindowType.Popup) or isinstance(obj, QMenu)
                if not (obj.windowFlags() & Qt.WindowType.ToolTip):
                    apply_window_effects(obj, small_corners=popup)
        return False


def fluent_palette() -> QPalette:
    """Palette for the parts Qt draws without the stylesheet (Fusion)."""
    accent = palette.accent()
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: palette.BASE,
        QPalette.ColorRole.WindowText: palette.TEXT,
        QPalette.ColorRole.Base: palette.CONTROL,
        QPalette.ColorRole.AlternateBase: palette.LAYER,
        QPalette.ColorRole.Text: palette.TEXT,
        QPalette.ColorRole.Button: palette.CONTROL,
        QPalette.ColorRole.ButtonText: palette.TEXT,
        QPalette.ColorRole.BrightText: palette.TEXT,
        QPalette.ColorRole.ToolTipBase: palette.FLYOUT,
        QPalette.ColorRole.ToolTipText: palette.TEXT,
        QPalette.ColorRole.Highlight: accent.fill,
        QPalette.ColorRole.HighlightedText: accent.on_fill,
        QPalette.ColorRole.Link: accent.text,
        QPalette.ColorRole.LinkVisited: accent.text,
        QPalette.ColorRole.PlaceholderText: palette.TEXT_TERTIARY,
        QPalette.ColorRole.Light: palette.themed("#3a3a3a"),
        QPalette.ColorRole.Midlight: palette.STROKE,
        QPalette.ColorRole.Mid: palette.themed("#2a2a2a"),
        QPalette.ColorRole.Dark: palette.BASE_ALT,
        QPalette.ColorRole.Shadow: "#000000",
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(palette.TEXT_DISABLED))
    return p


def prepare_mica(window: QWidget) -> None:
    """Call on the main window before it is shown, when Mica is on."""
    if _mica:
        window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)


def install(app: QApplication, mica: bool = False) -> FluentChrome:
    global _mica
    _mica = bool(mica) and mica_supported()
    app.setPalette(fluent_palette())
    chrome = FluentChrome(app)
    app.installEventFilter(chrome)
    return chrome

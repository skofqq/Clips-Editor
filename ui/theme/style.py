"""Fusion with the theme's own icons where Qt would draw its stock ones
(the ✕ that clears a line edit)."""

from __future__ import annotations

from PySide6.QtWidgets import QProxyStyle, QStyle


class AppStyle(QProxyStyle):
    def __init__(self) -> None:
        super().__init__("Fusion")

    def standardIcon(self, standard_icon, option=None, widget=None):  # noqa: N802
        if standard_icon == QStyle.StandardPixmap.SP_LineEditClearButton:
            from ui.theme import icon, palette  # noqa: PLC0415

            themed = icon("clear", palette.TEXT_SECONDARY)
            if not themed.isNull():
                return themed
        return super().standardIcon(standard_icon, option, widget)

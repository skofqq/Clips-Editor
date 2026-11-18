"""Friendly placeholder shown where a panel has nothing to show yet."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from ui.theme import icon, palette


class EmptyState(QWidget):
    """An icon, a short title, one line of help and an optional button."""

    action = Signal()

    def __init__(self, glyph: str, title: str, text: str = "", button: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("emptyState")
        self.icon_label = QLabel(self)
        self.icon_label.setPixmap(icon(glyph, palette.TEXT_TERTIARY).pixmap(QSize(40, 40)))
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("emptyTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text_label = QLabel(text, self)
        self.text_label.setObjectName("muted")
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text_label.setWordWrap(True)
        self.text_label.setVisible(bool(text))
        self.button = QPushButton(button, self)
        self.button.setVisible(bool(button))
        self.button.clicked.connect(self.action)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 24, 16, 24)
        layout.setSpacing(8)
        layout.addStretch(1)
        layout.addWidget(self.icon_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.text_label)
        layout.addSpacing(4)
        layout.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(2)

    def set_text(self, title: str, text: str = "") -> None:
        self.title_label.setText(title)
        self.text_label.setText(text)
        self.text_label.setVisible(bool(text))

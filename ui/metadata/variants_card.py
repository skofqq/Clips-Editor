"""Data page card: several titles and covers to pick from with one click."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QToolButton, QWidget

from ui.widgets.card import Card

TILE = QSize(84, 150)


class VariantsCard(Card):
    title_chosen = Signal(object)  # TitleVariant
    cover_chosen = Signal(object)  # CoverVariant
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Variants", "Other ways to write the title and other covers for this clip. Click one "
                         "to use it; the History tab shows which kind gets more views.", parent)
        self.titles_label = QLabel("<b>Titles</b>", self)
        self.body.addWidget(self.titles_label)
        self.titles_grid = QGridLayout()
        self.titles_grid.setHorizontalSpacing(10)
        self.titles_grid.setVerticalSpacing(4)
        self.titles_grid.setColumnStretch(1, 1)
        self.body.addLayout(self.titles_grid)
        self.titles_hint = QLabel("Fill in the clip title above to see title variants.", self)
        self.titles_hint.setObjectName("muted")
        self.body.addWidget(self.titles_hint)
        self.covers_label = QLabel("<b>Covers</b>", self)
        self.body.addWidget(self.covers_label)
        self.covers_row = QHBoxLayout()
        self.covers_row.setSpacing(8)
        self.covers_hint = QLabel("Covers appear when the suggested frames are ready.", self)
        self.covers_hint.setObjectName("muted")
        self.covers_row.addWidget(self.covers_hint)
        self.covers_row.addStretch(1)
        self.body.addLayout(self.covers_row)
        self.refresh_button = QPushButton("Make variants again", self)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.refresh_button)
        self.body.addLayout(row)
        self.refresh_button.clicked.connect(self.refresh_requested)
        self.title_buttons: list[QPushButton] = []
        self.cover_buttons: list[QToolButton] = []
        self._titles: list = []
        self._covers: list = []

    # ------------------------------------------------------------ titles
    def set_titles(self, variants: list, current: str = "") -> None:
        while self.titles_grid.count():
            widget = self.titles_grid.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self.title_buttons = []
        self._titles = list(variants)
        for row, v in enumerate(variants):
            label = QLabel(v.label, self)
            label.setObjectName("muted")
            button = QPushButton(v.title, self)
            button.setObjectName("variantTitle")
            button.setCheckable(True)
            button.setToolTip("Use this title (the description and tags follow it)")
            button.clicked.connect(lambda _c=False, item=v: self.title_chosen.emit(item))
            self.titles_grid.addWidget(label, row, 0)
            self.titles_grid.addWidget(button, row, 1)
            self.title_buttons.append(button)
        self.titles_hint.setVisible(not variants)
        self.mark_title(current)

    def mark_title(self, current: str) -> None:
        for button, v in zip(self.title_buttons, self._titles):
            button.setChecked(bool(current) and v.title == current)

    # ------------------------------------------------------------ covers
    def set_covers(self, variants: list, images: list[QImage], current=None) -> None:
        for button in self.cover_buttons:
            self.covers_row.removeWidget(button)
            button.deleteLater()
        self.cover_buttons = []
        self._covers = list(variants)
        for index, (v, image) in enumerate(zip(variants, images)):
            button = QToolButton(self)
            button.setObjectName("frameChoice")
            button.setCheckable(True)
            button.setAutoRaise(True)
            if not image.isNull():
                button.setIcon(QIcon(QPixmap.fromImage(image)))
            button.setIconSize(TILE)
            button.setToolTip(v.label)
            button.clicked.connect(lambda _c=False, item=v: self.cover_chosen.emit(item))
            self.covers_row.insertWidget(index, button)
            self.cover_buttons.append(button)
        self.covers_hint.setVisible(not variants)
        self.mark_cover(current)

    def mark_cover(self, design) -> None:
        for button, v in zip(self.cover_buttons, self._covers):
            button.setChecked(design is not None and v.design.source_us == design.source_us
                              and v.design.style == design.style)

    def title_texts(self) -> list[str]:
        return [v.title for v in self._titles]

    def cover_labels(self) -> list[str]:
        return [v.label for v in self._covers]

"""Publish page "History" tab: every Short sent to YouTube, TikTok or Instagram,
with its cover, title, date and link, and a way back to the clip it came from."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QWidget,
)

from core.projects.clip_memory import INSTAGRAM, SERVICE_TEXT, TIKTOK, YOUTUBE, ClipMemory, Publication
from ui.theme import icon
from ui.widgets.card import Card

ROLE = Qt.ItemDataRole.UserRole + 1
COVER = QSize(54, 96)  # a vertical Short
LOGOS = {YOUTUBE: "logo_youtube", TIKTOK: "logo_tiktok", INSTAGRAM: "logo_instagram"}


class PublishedPanel(Card):
    open_clip_requested = Signal(str)  # source path

    def __init__(self, memory: ClipMemory, parent: QWidget | None = None) -> None:
        super().__init__("Published Shorts", "Everything sent from this app, newest first. Double-click to open "
                         "the video; “Open clip” brings back the clip with its title and cover.", parent)
        self.memory = memory
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("Find by title")
        self.search_edit.setClearButtonEnabled(True)
        self.service_combo = QComboBox(self)
        self.service_combo.addItem("All services", "")
        for service in (YOUTUBE, TIKTOK, INSTAGRAM):
            self.service_combo.addItem(icon(LOGOS[service]), SERVICE_TEXT[service], service)
        top = QHBoxLayout()
        top.addWidget(self.search_edit, 1)
        top.addWidget(self.service_combo)
        self.body.addLayout(top)
        self.list = QListWidget(self)
        self.list.setObjectName("publishedList")
        self.list.setIconSize(COVER)
        self.list.setSpacing(2)
        self.list.setMinimumHeight(360)
        self.body.addWidget(self.list)
        self.empty_label = QLabel("Nothing published yet. Uploads and posts show up here.", self)
        self.empty_label.setObjectName("muted")
        self.body.addWidget(self.empty_label)
        self.open_button = QPushButton("Open video", self)
        self.clip_button = QPushButton("Open clip", self)
        self.clip_button.setToolTip("Import the clip this Short was made from, with its title, description, "
                                    "tags and cover")
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.clip_button)
        buttons.addWidget(self.open_button)
        self.body.addLayout(buttons)

        self.search_edit.textChanged.connect(lambda _t: self.refresh())
        self.service_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        self.list.itemDoubleClicked.connect(lambda _item: self.open_video())
        self.list.currentItemChanged.connect(lambda *_a: self._update_buttons())
        self.open_button.clicked.connect(self.open_video)
        self.clip_button.clicked.connect(self.open_clip)
        self.refresh()

    # ------------------------------------------------------------ list
    def refresh(self) -> None:
        text = self.search_edit.text().strip().casefold()
        service = self.service_combo.currentData()
        current = self.selected()
        self.list.clear()
        items = [p for p in self.memory.publications()
                 if (not service or p.service == service) and (not text or text in p.title.casefold())]
        for p in items:
            item = QListWidgetItem(self._cover(p), self._text(p))
            item.setData(ROLE, p)
            item.setToolTip(p.url)
            item.setSizeHint(QSize(0, COVER.height() + 12))
            self.list.addItem(item)
            if current is not None and p == current:
                self.list.setCurrentItem(item)
        self.empty_label.setVisible(not items)
        self.list.setVisible(bool(items))
        self._update_buttons()

    def _text(self, p: Publication) -> str:
        where = " · ".join(part for part in (SERVICE_TEXT.get(p.service, p.service), p.channel, p.privacy) if part)
        return f"{p.title or '(no title)'}\n{p.date_text} · {where}"

    def _cover(self, p: Publication) -> QIcon:
        path = self.memory.cover_of(p)
        if path:
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                return QIcon(pixmap.scaled(COVER, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))
        canvas = QPixmap(COVER)  # the logo in the middle of a cover-sized tile
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.drawPixmap((COVER.width() - 32) // 2, (COVER.height() - 32) // 2,
                           icon(LOGOS.get(p.service, "film")).pixmap(32, 32))
        painter.end()
        return QIcon(canvas)

    def selected(self) -> Publication | None:
        item = self.list.currentItem()
        return item.data(ROLE) if item is not None else None

    def _update_buttons(self) -> None:
        p = self.selected()
        self.open_button.setEnabled(p is not None and bool(p.url))
        source = self.memory.source_of(p) if p is not None else ""
        self.clip_button.setEnabled(bool(source) and Path(source).is_file())

    # ------------------------------------------------------------ actions
    def open_video(self) -> None:
        p = self.selected()
        if p is not None and p.url:
            QDesktopServices.openUrl(QUrl(p.url))

    def open_clip(self) -> None:
        p = self.selected()
        source = self.memory.source_of(p) if p is not None else ""
        if source and Path(source).is_file():
            self.open_clip_requested.emit(source)

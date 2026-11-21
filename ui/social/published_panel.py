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


NEWEST, VIEWS, LIKES, WATCHED = "newest", "views", "likes", "watched"
SORTS = ((NEWEST, "Newest first"), (VIEWS, "Most views"), (LIKES, "Most likes"), (WATCHED, "Best watched"))


def _count(n: int) -> str:
    """1234 → "1.2K", 2500000 → "2.5M"."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1000:.0f}K"
    if n >= 1000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(n)


class PublishedPanel(Card):
    open_clip_requested = Signal(str)  # source path
    refresh_requested = Signal()  # read the YouTube numbers again

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
        self.sort_combo = QComboBox(self)
        for key, text in SORTS:
            self.sort_combo.addItem(text, key)
        self.refresh_button = QPushButton("Refresh numbers", self)
        self.refresh_button.setToolTip("Read views, likes and watch time from YouTube")
        top = QHBoxLayout()
        top.addWidget(self.search_edit, 1)
        top.addWidget(self.service_combo)
        top.addWidget(self.sort_combo)
        top.addWidget(self.refresh_button)
        self.body.addLayout(top)
        self.summary_label = QLabel(self)
        self.summary_label.setObjectName("muted")
        self.summary_label.setWordWrap(True)
        self.body.addWidget(self.summary_label)
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
        self.sort_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        self.refresh_button.clicked.connect(self.refresh_requested)
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
        items = self._sorted(items)
        self._summarize(items)
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

    def _sorted(self, items: list[Publication]) -> list[Publication]:
        key = self.sort_combo.currentData()
        if key == NEWEST:
            return items
        def number(p: Publication) -> float:
            st = self.memory.stats_for(p)
            if st is None:
                return -1.0
            value = {VIEWS: st.views, LIKES: st.likes, WATCHED: st.avg_view_pct}.get(key)
            return -1.0 if value is None else float(value)
        return sorted(items, key=number, reverse=True)  # stable: newest first among equals

    def _summarize(self, items: list[Publication]) -> None:
        stats = [st for st in (self.memory.stats_for(p) for p in items) if st is not None]
        if not items:
            self.summary_label.setText("")
            return
        parts = [f"{len(items)} published"]
        if stats:
            views = sum(st.views for st in stats)
            parts.append(f"{_count(views)} views")
            parts.append(f"{_count(sum(st.likes for st in stats))} likes")
            watched = [(st.avg_view_pct, st.views) for st in stats if st.avg_view_pct is not None and st.views]
            if watched:
                total = sum(v for _pct, v in watched)
                parts.append(f"{sum(pct * v for pct, v in watched) / total:.0f}% watched on average")
            best = self._best_kind(items)
            if best:
                parts.append(best)
            fetched = max(st.fetched_at for st in stats)
            parts.append(f"numbers from {fetched[:16].replace('T', ' ')}")
        else:
            parts.append("press Refresh numbers to read views and likes from YouTube")
        self.summary_label.setText(" · ".join(parts))

    def _best_kind(self, items: list[Publication]) -> str:
        """The title kind with the most views per Short, when at least two kinds have numbers."""
        groups: dict[str, list[int]] = {}
        for p in items:
            st = self.memory.stats_for(p)
            if st is not None and p.variant:
                groups.setdefault(p.variant.split(" · ")[0], []).append(st.views)
        if len(groups) < 2:
            return ""
        kind, views = max(groups.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
        return f"best title kind: {kind} ({_count(int(sum(views) / len(views)))} views on average)"

    def set_busy(self, busy: bool, message: str = "") -> None:
        self.refresh_button.setEnabled(not busy)
        if busy:
            self.summary_label.setText(message or "Reading the numbers…")

    def set_note(self, text: str) -> None:
        """A problem reading the numbers; the list stays as it is."""
        self.refresh_button.setEnabled(True)
        self.summary_label.setText(text)

    def _text(self, p: Publication) -> str:
        where = " · ".join(part for part in (SERVICE_TEXT.get(p.service, p.service), p.channel, p.privacy) if part)
        text = f"{p.title or '(no title)'}\n{p.date_text} · {where}"
        if p.variant:
            text += f" · {p.variant}"
        st = self.memory.stats_for(p)
        if st is not None:
            numbers = [f"👁 {_count(st.views)}", f"♥ {_count(st.likes)}", f"💬 {_count(st.comments)}"]
            if st.avg_view_pct is not None:
                numbers.append(f"{st.avg_view_pct:.0f}% watched")
            if st.subscribers:
                numbers.append(f"+{st.subscribers} subscribers")
            text += "\n" + "   ".join(numbers)
        return text

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

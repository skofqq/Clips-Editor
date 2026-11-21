"""Analytics card on the Publish page: how the channel's Shorts do."""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.youtube.analytics import Summary, VideoStats, summarize
from ui.widgets.card import Card

COLUMNS = ("Title", "Published", "Views", "Likes", "Like %", "Comments", "Watched %", "Subscribers")


def _count(value: int | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1000:.0f}K"
    if value >= 1000:
        return f"{value / 1000:.1f}K"
    return str(value)


class _Item(QTableWidgetItem):
    """A cell that sorts by its number, not its text."""

    def __init__(self, text: str, key) -> None:
        super().__init__(text)
        self.key = key
        self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def __lt__(self, other) -> bool:  # noqa: D105
        if not isinstance(other, _Item):
            return super().__lt__(other)
        if self.key is None or other.key is None:  # "—" sorts below every number
            return self.key is None and other.key is not None
        return self.key < other.key


class _Tile(QWidget):
    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statTile")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        column = QVBoxLayout(self)
        column.setContentsMargins(14, 10, 14, 12)
        column.setSpacing(0)
        self.value = QLabel("—", self)
        self.value.setObjectName("statValue")
        label = QLabel(caption, self)
        label.setObjectName("muted")
        column.addWidget(self.value)
        column.addWidget(label)


class AnalyticsPanel(Card):
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Analytics", "How your Shorts do on YouTube: views, likes and how much of each video "
                         "people watch. Double-click a row to open the video.", parent)
        self._stats: list[VideoStats] = []
        self.channel_label = QLabel(self)
        self.channel_label.setObjectName("muted")
        self.shorts_check = QCheckBox("Only Shorts", self)
        self.shorts_check.setChecked(True)
        self.app_check = QCheckBox("Only uploaded with this app", self)
        self.refresh_button = QPushButton("Refresh", self)
        self.refresh_button.setObjectName("primary")
        top = QHBoxLayout()
        top.addWidget(self.channel_label, 1)
        top.addWidget(self.shorts_check)
        top.addWidget(self.app_check)
        top.addWidget(self.refresh_button)
        self.body.addLayout(top)

        tiles = QGridLayout()
        self.tiles = {key: _Tile(caption, self) for key, caption in (
            ("videos", "Videos"), ("views", "Views"), ("likes", "Likes"),
            ("watched", "Watched on average"), ("subscribers", "Subscribers gained"))}
        for i, tile in enumerate(self.tiles.values()):
            tiles.addWidget(tile, 0, i)
        self.body.addLayout(tiles)
        self.best_label = QLabel(self)
        self.best_label.setWordWrap(True)
        self.body.addWidget(self.best_label)

        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setMinimumHeight(260)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSortIndicator(1, Qt.SortOrder.DescendingOrder)  # newest first
        self.body.addWidget(self.table)
        self.status = QLabel(self)
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        self.status.setOpenExternalLinks(True)
        self.body.addWidget(self.status)

        self.refresh_button.clicked.connect(self.refresh_requested)
        self.shorts_check.toggled.connect(lambda _on: self._show())
        self.app_check.toggled.connect(lambda _on: self._show())
        self.table.cellDoubleClicked.connect(self._open)
        self._note = ""
        self._when = ""

    # ------------------------------------------------------------ state
    def set_channel(self, title: str) -> None:
        self.channel_label.setText(f"Channel: {title}" if title else "No channel connected")
        self.refresh_button.setEnabled(bool(title))

    def set_busy(self, busy: bool, message: str = "") -> None:
        self.refresh_button.setEnabled(not busy)
        self.refresh_button.setText("Loading…" if busy else "Refresh")
        if message:
            self.status.setText(message)

    def set_error(self, message: str) -> None:
        self.set_busy(False)
        self.status.setText(message)

    def set_stats(self, stats: list[VideoStats], note: str = "", when: datetime | None = None) -> None:
        self._stats = list(stats)
        self._note = note
        self._when = (when or datetime.now()).strftime("%d.%m.%Y %H:%M")
        self.set_busy(False)
        self._show()

    def shown(self) -> list[VideoStats]:
        stats = self._stats
        if self.shorts_check.isChecked():
            stats = [s for s in stats if s.is_short]
        if self.app_check.isChecked():
            stats = [s for s in stats if s.from_app]
        return stats

    def _show(self) -> None:
        stats = self.shown()
        self._show_summary(summarize(stats))
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(stats))
        for row, s in enumerate(stats):
            published = s.published_at[:10]
            title = _Item(("● " if s.from_app else "") + s.title, s.title.casefold())
            title.setToolTip(s.url + ("\nUploaded with this app" if s.from_app else ""))
            title.setData(Qt.ItemDataRole.UserRole, s.url)
            cells = [
                title,
                _Item(".".join(reversed(published.split("-"))) if published else "", s.published_at),
                _Item(_count(s.views), s.views),
                _Item(_count(s.likes), s.likes),
                _Item(f"{s.like_rate:.1f}" if s.views else "—", s.like_rate if s.views else None),
                _Item(_count(s.comments), s.comments),
                _Item(f"{s.avg_view_pct:.0f}%" if s.avg_view_pct is not None else "—", s.avg_view_pct),
                _Item(_count(s.subscribers), s.subscribers),
            ]
            for column, item in enumerate(cells):
                if column:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        parts = [f"Updated {self._when}" if self._when else ""]
        if self._stats and not stats:
            parts.append("No videos match the filters.")
        if self._note:
            parts.append(self._note)
        self.status.setText("  ·  ".join(p for p in parts if p))

    def _show_summary(self, summary: Summary) -> None:
        self.tiles["videos"].value.setText(str(summary.videos) if summary.videos else "—")
        self.tiles["views"].value.setText(_count(summary.views) if summary.videos else "—")
        self.tiles["likes"].value.setText(_count(summary.likes) if summary.videos else "—")
        self.tiles["watched"].value.setText(f"{summary.avg_view_pct:.0f}%" if summary.avg_view_pct is not None
                                            else "—")
        self.tiles["subscribers"].value.setText(_count(summary.subscribers))
        best = summary.best
        self.best_label.setText(f"Best: <b>{html.escape(best.title)}</b> — {_count(best.views)} views" if best and best.views
                                else "")

    def _open(self, row: int, _column: int) -> None:
        item = self.table.item(row, 0)
        url = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if url:
            QDesktopServices.openUrl(QUrl(url))

"""Left panel: imported videos with thumbnails.

Empty, it shows a large "drop a video here" hint with an Import button.
Each row has a thumbnail, the file name, the length and the resolution.
Right-click a row (or use the small Remove button) to take it out of the
project; the file itself is never touched.

Clips are grouped by month and day (newest first); click a group's header to
fold it. The calendar button picks the grouping and which date counts (the
clip's own date or the day it was imported); both and the folded groups are
remembered (core/media/dates.MediaGroupsStore).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.media import dates
from core.media.dates import MediaGroupsStore
from core.media.probe import MediaInfo, format_duration
from ui.theme import icon, palette
from ui.widgets.empty_state import EmptyState

_ROLE_MEDIA = Qt.ItemDataRole.UserRole + 1
_ROLE_THUMB = Qt.ItemDataRole.UserRole + 2
_ROLE_KIND = Qt.ItemDataRole.UserRole + 3   # KIND_MONTH / KIND_DAY for group headers, None for clips
_ROLE_KEY = Qt.ItemDataRole.UserRole + 4    # "2026-11" / "2026-11-22"
_ROLE_COUNT = Qt.ItemDataRole.UserRole + 5  # clips in the group
_ROLE_COLLAPSED = Qt.ItemDataRole.UserRole + 6
KIND_MONTH = "month"
KIND_DAY = "day"
ROW_H = 68
MONTH_H, DAY_H = 34, 28
THUMB_W, THUMB_H = 88, 50


class MediaDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        kind = index.data(_ROLE_KIND)
        height = MONTH_H if kind == KIND_MONTH else DAY_H if kind == KIND_DAY else ROW_H
        return QSize(option.rect.width(), height)

    def paint(self, p: QPainter, option, index) -> None:
        if index.data(_ROLE_KIND):
            self._paint_header(p, option, index)
            return
        info: MediaInfo = index.data(_ROLE_MEDIA)
        thumb: QPixmap | None = index.data(_ROLE_THUMB)
        rect = QRectF(option.rect).adjusted(2, 2, -2, -2)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(palette.CONTROL_HOVER if selected else palette.SUBTLE_HOVER))
            p.drawRoundedRect(rect, 6, 6)
        if selected:
            p.setBrush(QColor(palette.accent().text))
            p.drawRoundedRect(QRectF(rect.left(), rect.center().y() - 8, 3, 16), 1.5, 1.5)

        box = QRectF(rect.left() + 10, rect.center().y() - THUMB_H / 2, THUMB_W, THUMB_H)
        clip = QPainterPath()
        clip.addRoundedRect(box, 4, 4)
        p.fillPath(clip, QColor(palette.BASE_ALT))
        if thumb is not None and not thumb.isNull():
            p.setClipPath(clip)
            scaled = thumb.scaled(box.size().toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                  Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(int(box.center().x() - scaled.width() / 2), int(box.center().y() - scaled.height() / 2),
                         scaled)
            p.setClipping(False)
        else:
            p.drawPixmap(int(box.center().x() - 10), int(box.center().y() - 10),
                         icon("film", palette.TEXT_DISABLED).pixmap(20, 20))
        p.setPen(QColor(palette.STROKE))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)

        text_left = box.right() + 10
        text_w = rect.right() - text_left - 6
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(QColor(palette.TEXT))
        name = p.fontMetrics().elidedText(info.path.name, Qt.TextElideMode.ElideMiddle, int(text_w))
        p.drawText(QRectF(text_left, rect.top() + 12, text_w, 20), Qt.AlignmentFlag.AlignLeft, name)
        p.setFont(option.font)
        p.setPen(QColor(palette.TEXT_TERTIARY))
        p.drawText(QRectF(text_left, rect.top() + 33, text_w, 20), Qt.AlignmentFlag.AlignLeft,
                   MediaPanel.details(info))
        p.restore()

    def _paint_header(self, p: QPainter, option, index) -> None:
        """A group header: chevron, "November 2026" / "Fri, 20 Nov", clip count."""
        month = index.data(_ROLE_KIND) == KIND_MONTH
        collapsed = bool(index.data(_ROLE_COLLAPSED))
        rect = QRectF(option.rect).adjusted(2, 1, -2, -1)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if option.state & QStyle.StateFlag.State_MouseOver or option.state & QStyle.StateFlag.State_HasFocus:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(palette.SUBTLE_HOVER))
            p.drawRoundedRect(rect, 6, 6)
        left = rect.left() + (6 if month else 18)
        cy = rect.center().y()
        color = QColor(palette.TEXT if month else palette.TEXT_SECONDARY)
        pen = QPen(color, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        cx = left + 6
        if collapsed:  # ›
            points = [QPointF(cx - 2, cy - 4), QPointF(cx + 2, cy), QPointF(cx - 2, cy + 4)]
        else:  # ⌄
            points = [QPointF(cx - 4, cy - 2), QPointF(cx, cy + 2), QPointF(cx + 4, cy - 2)]
        p.drawPolyline(points)

        count = f"{index.data(_ROLE_COUNT) or 0}"
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold if month else QFont.Weight.Medium)
        if month:
            font.setPointSizeF(font.pointSizeF() * 1.08)
        p.setFont(font)
        metrics = p.fontMetrics()
        text_left = left + 18
        count_w = metrics.horizontalAdvance(count) + 14
        text_w = rect.right() - text_left - count_w - 16
        p.setPen(color)
        label = metrics.elidedText(index.data(Qt.ItemDataRole.DisplayRole) or "", Qt.TextElideMode.ElideRight,
                                   int(text_w))
        p.drawText(QRectF(text_left, rect.top(), text_w, rect.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
        # Count in a small pill on the right.
        font.setWeight(QFont.Weight.Medium)
        font.setPointSizeF(option.font.pointSizeF() * 0.92)
        p.setFont(font)
        pill = QRectF(rect.right() - count_w - 12, cy - 9, count_w, 18)  # clear of the overlay scrollbar
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(palette.CONTROL))
        p.drawRoundedRect(pill, 9, 9)
        p.setPen(QColor(palette.TEXT_SECONDARY))
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, count)
        p.restore()


class MediaPanel(QWidget):
    import_requested = Signal()
    media_selected = Signal(object)  # MediaInfo | None
    remove_requested = Signal(object)  # MediaInfo

    def __init__(self, parent: QWidget | None = None, store: MediaGroupsStore | None = None) -> None:
        super().__init__(parent)
        self.store = store or MediaGroupsStore()
        self._media: list[MediaInfo] = []           # import order
        self._thumbs: dict[str, QPixmap] = {}       # path key → thumbnail
        self._times: dict[str, object] = {}         # path key → datetime (cached per date source)
        self._selected: MediaInfo | None = None     # last clip announced through media_selected

        self.list = QListWidget(self)
        self.list.setObjectName("mediaList")
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list.setItemDelegate(MediaDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.viewport().installEventFilter(self)
        self.list.installEventFilter(self)

        self.empty = EmptyState(
            "film", "No videos yet", "Drop a Twitch clip here or import one from disk.", "Import video…", self
        )
        self.empty.button.setObjectName("primary")
        self.import_button = self.empty.button

        self.count_label = QLabel(self)
        self.count_label.setObjectName("muted")
        self.group_button = self._tool("calendar", "Group by date")
        self.group_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.group_menu = self._build_group_menu()
        self.group_button.setMenu(self.group_menu)
        self.add_button = self._tool("import", "Import more videos  (Ctrl+I)")
        self.remove_button = self._tool("remove", "Remove from project (the file stays on disk)")
        self.remove_button.setEnabled(False)
        footer = QHBoxLayout()
        footer.setContentsMargins(4, 0, 0, 0)
        footer.addWidget(self.count_label, 1)
        footer.addWidget(self.group_button)
        footer.addWidget(self.add_button)
        footer.addWidget(self.remove_button)

        list_page = QWidget(self)
        list_layout = QVBoxLayout(list_page)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(4)
        list_layout.addWidget(self.list, 1)
        list_layout.addLayout(footer)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(list_page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.stack, 1)

        self.empty.action.connect(self.import_requested)
        self.add_button.clicked.connect(self.import_requested)
        self.remove_button.clicked.connect(self._on_remove)
        self.list.currentItemChanged.connect(self._on_current_changed)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self._update_state()

    def _tool(self, glyph: str, tip: str) -> QToolButton:
        button = QToolButton(self)
        button.setIcon(icon(glyph))
        button.setIconSize(QSize(18, 18))
        button.setToolTip(tip)
        button.setAccessibleName(tip.split("  (")[0])
        return button

    def _build_group_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addSection("Group by")
        self._grouping_actions: dict[str, QAction] = {}
        group = QActionGroup(menu)
        for key, text in ((dates.GROUP_MONTH_DAY, "Month and day"), (dates.GROUP_MONTH, "Month"),
                          (dates.GROUP_DAY, "Day"), (dates.GROUP_NONE, "No grouping")):
            action = menu.addAction(text)
            action.setCheckable(True)
            action.setActionGroup(group)
            action.triggered.connect(lambda _=False, k=key: self.set_grouping(k))
            self._grouping_actions[key] = action
        menu.addSection("Date")
        self._source_actions: dict[str, QAction] = {}
        group = QActionGroup(menu)
        for key, text, tip in (
            (dates.SOURCE_CLIP, "Clip date",
             "The date in the file name (usually the stream date), otherwise when the file was saved"),
            (dates.SOURCE_IMPORT, "Import date", "The day the clip was first imported"),
        ):
            action = menu.addAction(text)
            action.setToolTip(tip)
            action.setCheckable(True)
            action.setActionGroup(group)
            action.triggered.connect(lambda _=False, k=key: self.set_date_source(k))
            self._source_actions[key] = action
        menu.setToolTipsVisible(True)
        menu.addSeparator()
        self.act_collapse_all = menu.addAction("Collapse all", lambda: self.set_all_collapsed(True))
        self.act_expand_all = menu.addAction("Expand all", lambda: self.set_all_collapsed(False))
        menu.aboutToShow.connect(self._sync_group_menu)
        return menu

    def _sync_group_menu(self) -> None:
        for key, action in self._grouping_actions.items():
            action.setChecked(key == self.store.grouping)
        for key, action in self._source_actions.items():
            action.setChecked(key == self.store.source)
            action.setEnabled(self.store.grouping != dates.GROUP_NONE)
        grouped = self.store.grouping != dates.GROUP_NONE
        self.act_collapse_all.setEnabled(grouped and bool(self._media))
        self.act_expand_all.setEnabled(grouped and bool(self._media))

    # ------------------------------------------------------------- data
    def set_store(self, store: MediaGroupsStore) -> None:
        """Use (and save to) ``store``: grouping, date source, folded groups, import days."""
        self.store = store
        changed = False
        for info in self._media:
            changed |= store.note_import(info.path)
        if changed:
            store.save()
        self._times.clear()
        self._rebuild()

    def add_media(self, info: MediaInfo, select: bool = True) -> None:
        existing = self._find(info.path)
        if existing is not None:
            if select:
                self._reveal(existing)
                self.list.setCurrentItem(existing)
            return
        self._media.append(info)
        if self.store.note_import(info.path):
            self.store.save()
        self._rebuild()
        if select:
            item = self._find(info.path)
            self._reveal(item)
            self.list.setCurrentItem(item)

    def set_thumbnail(self, path: Path, image) -> None:
        if image is None or self._info(path) is None:
            return
        pixmap = image if isinstance(image, QPixmap) else QPixmap.fromImage(image)
        self._thumbs[dates.path_key(path)] = pixmap
        item = self._find(path)
        if item is not None:
            item.setData(_ROLE_THUMB, pixmap)

    def thumbnail(self, path: Path) -> QPixmap | None:
        return self._thumbs.get(dates.path_key(path)) if self._info(path) is not None else None

    def remove_media(self, path: Path) -> None:
        info = self._info(path)
        if info is not None:
            self._media.remove(info)
            self._thumbs.pop(dates.path_key(path), None)
            if self._selected is info:
                self._selected = None
            self._rebuild()
        self._update_state()

    def clear(self) -> None:
        self._media.clear()
        self._thumbs.clear()
        self._selected = None
        self.list.clear()
        self._update_state()

    def all_media(self) -> list[MediaInfo]:
        return list(self._media)

    def media_count(self) -> int:
        return len(self._media)

    def current_media(self) -> MediaInfo | None:
        item = self.list.currentItem()
        if item is not None and not item.data(_ROLE_KIND):
            return item.data(_ROLE_MEDIA)
        return self._selected

    def select_media(self, path: Path) -> bool:
        """Make the clip at ``path`` current (unfolding its group); False if absent."""
        item = self._find(path)
        if item is None:
            return False
        self._reveal(item)
        self.list.setCurrentItem(item)
        return True

    def media_item(self, path: Path) -> QListWidgetItem | None:
        return self._find(path)

    # ---------------------------------------------------------- grouping
    def set_grouping(self, grouping: str) -> None:
        if grouping not in dates.GROUPINGS or grouping == self.store.grouping:
            return
        self.store.grouping = grouping
        self.store.save()
        self._rebuild()

    def set_date_source(self, source: str) -> None:
        if source not in dates.SOURCES or source == self.store.source:
            return
        self.store.source = source
        self.store.save()
        self._times.clear()
        self._rebuild()

    def headers(self) -> list[QListWidgetItem]:
        return [self.list.item(i) for i in range(self.list.count()) if self.list.item(i).data(_ROLE_KIND)]

    def is_collapsed(self, key: str) -> bool:
        return key in self.store.collapsed

    def set_collapsed(self, key: str, collapsed: bool) -> None:
        if collapsed == (key in self.store.collapsed):
            return
        if collapsed:
            self.store.collapsed.add(key)
        else:
            self.store.collapsed.discard(key)
        self.store.save()
        self._apply_folding()

    def toggle_group(self, key: str) -> None:
        self.set_collapsed(key, key not in self.store.collapsed)

    def set_all_collapsed(self, collapsed: bool) -> None:
        keys = {item.data(_ROLE_KEY) for item in self.headers()}
        if self.store.grouping == dates.GROUP_MONTH_DAY and collapsed:
            keys = {item.data(_ROLE_KEY) for item in self.headers() if item.data(_ROLE_KIND) == KIND_MONTH}
            # Folding months alone keeps the days' own state for when a month opens again.
        if collapsed:
            self.store.collapsed |= keys
        else:
            self.store.collapsed -= keys
        self.store.save()
        self._apply_folding()

    def clip_time(self, info: MediaInfo):
        key = dates.path_key(info.path)
        if key not in self._times:
            self._times[key] = self.store.clip_time(info.path)
        return self._times[key]

    def _rebuild(self) -> None:
        """Lay the list out again: headers + clips, newest group first."""
        current = self.current_media()
        self.list.blockSignals(True)
        try:
            self.list.clear()
            grouping = self.store.grouping
            if grouping == dates.GROUP_NONE:
                for info in self._media:
                    self.list.addItem(self._media_item(info))
            else:
                ordered = sorted(self._media, key=self.clip_time, reverse=True)
                months: dict[str, int] = {}
                days: dict[str, int] = {}
                for info in ordered:
                    day = self.clip_time(info).date()
                    months[dates.month_key(day)] = months.get(dates.month_key(day), 0) + 1
                    days[dates.day_key(day)] = days.get(dates.day_key(day), 0) + 1
                last_month = last_day = None
                for info in ordered:
                    day = self.clip_time(info).date()
                    month_key, day_key = dates.month_key(day), dates.day_key(day)
                    if grouping in (dates.GROUP_MONTH_DAY, dates.GROUP_MONTH) and month_key != last_month:
                        self.list.addItem(self._header(KIND_MONTH, month_key, dates.month_label(month_key),
                                                       months[month_key]))
                        last_month = month_key
                    if grouping in (dates.GROUP_MONTH_DAY, dates.GROUP_DAY) and day_key != last_day:
                        self.list.addItem(self._header(KIND_DAY, day_key, dates.day_label(day_key),
                                                       days[day_key]))
                        last_day = day_key
                    self.list.addItem(self._media_item(info))
            self._apply_folding()
            if current is not None:
                item = self._find(current.path)
                if item is not None:
                    self.list.setCurrentItem(item)
        finally:
            self.list.blockSignals(False)
        self._update_state()
        self.remove_button.setEnabled(self.current_media() is not None)

    def _media_item(self, info: MediaInfo) -> QListWidgetItem:
        item = QListWidgetItem(self._label(info))
        item.setData(_ROLE_MEDIA, info)
        thumb = self._thumbs.get(dates.path_key(info.path))
        if thumb is not None:
            item.setData(_ROLE_THUMB, thumb)
        tip = str(info.path)
        if info.video:
            tip += f"\n{format_duration(info.duration_s)} · {info.video.width}×{info.video.height}"
        when = self.clip_time(info)
        tip += f"\n{'Imported' if self.store.source == dates.SOURCE_IMPORT else 'Date'}: {when:%d.%m.%Y %H:%M}"
        item.setToolTip(tip)
        return item

    def _header(self, kind: str, key: str, label: str, count: int) -> QListWidgetItem:
        item = QListWidgetItem(label)
        item.setData(_ROLE_KIND, kind)
        item.setData(_ROLE_KEY, key)
        item.setData(_ROLE_COUNT, count)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setToolTip(f"{count} clip{'s' if count != 1 else ''} · click to fold or unfold")
        return item

    def _apply_folding(self) -> None:
        """Hide the rows under folded headers; mark headers folded for painting."""
        month_closed = day_closed = False
        for i in range(self.list.count()):
            item = self.list.item(i)
            kind = item.data(_ROLE_KIND)
            if kind == KIND_MONTH:
                month_closed = item.data(_ROLE_KEY) in self.store.collapsed
                day_closed = False
                item.setData(_ROLE_COLLAPSED, month_closed)
                item.setHidden(False)
            elif kind == KIND_DAY:
                day_closed = item.data(_ROLE_KEY) in self.store.collapsed
                item.setData(_ROLE_COLLAPSED, day_closed)
                item.setHidden(month_closed)
            else:
                item.setHidden(month_closed or day_closed)

    def _reveal(self, item: QListWidgetItem | None) -> None:
        """Unfold the groups holding ``item`` so it can be seen."""
        if item is None or not item.isHidden():
            return
        when = self.clip_time(item.data(_ROLE_MEDIA)).date()
        before = len(self.store.collapsed)
        self.store.collapsed -= {dates.month_key(when), dates.day_key(when)}
        if len(self.store.collapsed) != before:
            self.store.save()
        self._apply_folding()

    @staticmethod
    def details(info: MediaInfo) -> str:
        """Short line under the name: "0:08 · 1080p"."""
        seconds = int(round(info.duration_s))
        hours, rest = divmod(seconds, 3600)
        length = f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"
        details = [length]
        if info.video:
            short_side = min(info.video.width, info.video.height)
            details.append(f"{short_side}p" if short_side in (360, 480, 720, 1080, 1440, 2160)
                           else f"{info.video.width}×{info.video.height}")
        return " · ".join(details)

    @classmethod
    def _label(cls, info: MediaInfo) -> str:
        return f"{info.path.name}\n{cls.details(info)}"

    def _info(self, path: Path) -> MediaInfo | None:
        key = dates.path_key(path)
        return next((m for m in self._media if dates.path_key(m.path) == key), None)

    def _find(self, path: Path) -> QListWidgetItem | None:
        key = dates.path_key(path)
        for i in range(self.list.count()):
            item = self.list.item(i)
            info = item.data(_ROLE_MEDIA)
            if info is not None and dates.path_key(info.path) == key:
                return item
        return None

    # ---------------------------------------------------------- handlers
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.list.viewport() and event.type() in (QEvent.Type.MouseButtonPress,
                                                           QEvent.Type.MouseButtonDblClick):
            if event.button() == Qt.MouseButton.LeftButton:
                item = self.list.itemAt(event.position().toPoint())
                if item is not None and item.data(_ROLE_KIND):
                    self.toggle_group(item.data(_ROLE_KEY))
                    return True
        elif obj is self.list and event.type() == QEvent.Type.KeyPress:
            item = self.list.currentItem()
            if item is not None and item.data(_ROLE_KIND):
                key = event.key()
                if key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self.toggle_group(item.data(_ROLE_KEY))
                    return True
                if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                    self.set_collapsed(item.data(_ROLE_KEY), key == Qt.Key.Key_Left)
                    return True
        return super().eventFilter(obj, event)

    def _update_state(self) -> None:
        count = len(self._media)
        self.stack.setCurrentIndex(1 if count else 0)
        self.count_label.setText(f"{count} video{'s' if count != 1 else ''}")

    def _on_current_changed(self, current: QListWidgetItem | None, _previous) -> None:
        if current is not None and current.data(_ROLE_KIND):
            return  # keyboard focus on a group header: the open clip stays
        info = current.data(_ROLE_MEDIA) if current else None
        self.remove_button.setEnabled(info is not None)
        self._selected = info
        self.media_selected.emit(info)

    def _on_remove(self) -> None:
        info = self.current_media()
        if info is not None:
            self.remove_requested.emit(info)

    def _context_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        menu = QMenu(self)
        if item is not None and item.data(_ROLE_KIND):
            key = item.data(_ROLE_KEY)
            menu.addAction("Expand" if self.is_collapsed(key) else "Collapse", lambda: self.toggle_group(key))
            menu.addSeparator()
        elif item is not None:
            self.list.setCurrentItem(item)
            menu.addAction(icon("remove"), "Remove from project", self._on_remove)
            menu.addSeparator()
        menu.addAction(icon("import"), "Import video…", self.import_requested.emit)
        if self._media:
            menu.addSeparator()
            self._sync_group_menu()
            group_menu = menu.addMenu(icon("calendar"), "Group by date")
            for action in self.group_menu.actions():
                group_menu.addAction(action)
        menu.exec(self.list.viewport().mapToGlobal(pos))

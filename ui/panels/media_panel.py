"""Left panel: imported videos with thumbnails.

Empty, it shows a large "drop a video here" hint with an Import button.
Each row has a thumbnail, the file name, the length and the resolution.
Right-click a row (or use the small Remove button) to take it out of the
project; the file itself is never touched.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
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

from core.media.probe import MediaInfo, format_duration
from ui.theme import icon, palette
from ui.widgets.empty_state import EmptyState

_ROLE_MEDIA = Qt.ItemDataRole.UserRole + 1
_ROLE_THUMB = Qt.ItemDataRole.UserRole + 2
ROW_H = 68
THUMB_W, THUMB_H = 88, 50


class MediaDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), ROW_H)

    def paint(self, p: QPainter, option, index) -> None:
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


class MediaPanel(QWidget):
    import_requested = Signal()
    media_selected = Signal(object)  # MediaInfo | None
    remove_requested = Signal(object)  # MediaInfo

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.list = QListWidget(self)
        self.list.setObjectName("mediaList")
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list.setItemDelegate(MediaDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.empty = EmptyState(
            "film", "No videos yet", "Drop a Twitch clip here or import one from disk.", "Import video…", self
        )
        self.empty.button.setObjectName("primary")
        self.import_button = self.empty.button

        self.count_label = QLabel(self)
        self.count_label.setObjectName("muted")
        self.add_button = self._tool("import", "Import more videos  (Ctrl+I)")
        self.remove_button = self._tool("remove", "Remove from project (the file stays on disk)")
        self.remove_button.setEnabled(False)
        footer = QHBoxLayout()
        footer.setContentsMargins(4, 0, 0, 0)
        footer.addWidget(self.count_label, 1)
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

    # ------------------------------------------------------------- data
    def add_media(self, info: MediaInfo, select: bool = True) -> None:
        existing = self._find(info.path)
        if existing is not None:
            if select:
                self.list.setCurrentItem(existing)
            return
        item = QListWidgetItem(self._label(info))
        item.setData(_ROLE_MEDIA, info)
        tip = str(info.path)
        if info.video:
            tip += f"\n{format_duration(info.duration_s)} · {info.video.width}×{info.video.height}"
        item.setToolTip(tip)
        self.list.addItem(item)
        self._update_state()
        if select:
            self.list.setCurrentItem(item)

    def set_thumbnail(self, path: Path, image) -> None:
        item = self._find(path)
        if item is not None and image is not None:
            pixmap = image if isinstance(image, QPixmap) else QPixmap.fromImage(image)
            item.setData(_ROLE_THUMB, pixmap)

    def thumbnail(self, path: Path) -> QPixmap | None:
        item = self._find(path)
        return item.data(_ROLE_THUMB) if item else None

    def remove_media(self, path: Path) -> None:
        item = self._find(path)
        if item is not None:
            self.list.takeItem(self.list.row(item))
        self._update_state()

    def clear(self) -> None:
        self.list.clear()
        self._update_state()

    def all_media(self) -> list[MediaInfo]:
        return [self.list.item(i).data(_ROLE_MEDIA) for i in range(self.list.count())]

    def current_media(self) -> MediaInfo | None:
        item = self.list.currentItem()
        return item.data(_ROLE_MEDIA) if item else None

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

    def _find(self, path: Path) -> QListWidgetItem | None:
        key = str(path).casefold()
        for i in range(self.list.count()):
            item = self.list.item(i)
            if str(item.data(_ROLE_MEDIA).path).casefold() == key:
                return item
        return None

    # ---------------------------------------------------------- handlers
    def _update_state(self) -> None:
        count = self.list.count()
        self.stack.setCurrentIndex(1 if count else 0)
        self.count_label.setText(f"{count} video{'s' if count != 1 else ''}")

    def _on_current_changed(self, current: QListWidgetItem | None, _previous) -> None:
        self.remove_button.setEnabled(current is not None)
        self.media_selected.emit(current.data(_ROLE_MEDIA) if current else None)

    def _on_remove(self) -> None:
        info = self.current_media()
        if info is not None:
            self.remove_requested.emit(info)

    def _context_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        menu = QMenu(self)
        if item is not None:
            self.list.setCurrentItem(item)
            menu.addAction(icon("remove"), "Remove from project", self._on_remove)
            menu.addSeparator()
        menu.addAction(icon("import"), "Import video…", self.import_requested.emit)
        menu.exec(self.list.viewport().mapToGlobal(pos))

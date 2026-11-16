"""Left panel: imported media list."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.media.probe import MediaInfo, format_duration

_ROLE_MEDIA = Qt.ItemDataRole.UserRole + 1


class MediaPanel(QWidget):
    import_requested = Signal()
    media_selected = Signal(object)  # MediaInfo | None
    remove_requested = Signal(object)  # MediaInfo

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.list = QListWidget(self)
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)

        self.import_button = QPushButton("Import…", self)
        self.import_button.setObjectName("primary")
        self.remove_button = QPushButton("Remove", self)
        self.remove_button.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.import_button, 1)
        buttons.addWidget(self.remove_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addLayout(buttons)
        layout.addWidget(self.list, 1)

        self.import_button.clicked.connect(self.import_requested)
        self.remove_button.clicked.connect(self._on_remove)
        self.list.currentItemChanged.connect(self._on_current_changed)

    def add_media(self, info: MediaInfo, select: bool = True) -> None:
        existing = self._find(info.path)
        if existing is not None:
            if select:
                self.list.setCurrentItem(existing)
            return
        item = QListWidgetItem(self._label(info))
        item.setData(_ROLE_MEDIA, info)
        item.setToolTip(str(info.path))
        self.list.addItem(item)
        if select:
            self.list.setCurrentItem(item)

    def remove_media(self, path: Path) -> None:
        item = self._find(path)
        if item is not None:
            self.list.takeItem(self.list.row(item))

    def clear(self) -> None:
        self.list.clear()

    def all_media(self) -> list[MediaInfo]:
        return [self.list.item(i).data(_ROLE_MEDIA) for i in range(self.list.count())]

    def current_media(self) -> MediaInfo | None:
        item = self.list.currentItem()
        return item.data(_ROLE_MEDIA) if item else None

    @staticmethod
    def _label(info: MediaInfo) -> str:
        details = [format_duration(info.duration_s)]
        if info.video:
            details.append(f"{info.video.width}×{info.video.height}")
        return f"{info.path.name}\n{' · '.join(details)}"

    def _find(self, path: Path) -> QListWidgetItem | None:
        key = str(path).casefold()
        for i in range(self.list.count()):
            item = self.list.item(i)
            if str(item.data(_ROLE_MEDIA).path).casefold() == key:
                return item
        return None

    def _on_current_changed(self, current: QListWidgetItem | None, _previous) -> None:
        self.remove_button.setEnabled(current is not None)
        self.media_selected.emit(current.data(_ROLE_MEDIA) if current else None)

    def _on_remove(self) -> None:
        info = self.current_media()
        if info is not None:
            self.remove_requested.emit(info)

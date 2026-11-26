"""Chips: a row of removable tokens plus a text field with suggestions.

``ChipInput`` holds ``(id, label)`` pairs. Typing shows matching suggestions;
Enter or a click on a suggestion adds a chip. Text that matches no
suggestion either becomes a chip as it is (``free_text``, used for tags) or
asks the owner to create a profile (``create_requested``). Double-clicking a
chip asks to edit that profile. ``single`` keeps at most one chip.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, QStringListModel, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QSizePolicy,
    QToolButton,
    QWidget,
    QWidgetItem,
)

from ui.theme import palette

CHIP_STYLE = (
    "QWidget#chipInput { background: #2d2d2d; border: 1px solid #363636; border-bottom: 1px solid #8a8a8a;"
    " border-radius: 4px; }"
    "QWidget#chipInput:hover { background: #323232; }"
    "QWidget#chipInput QLineEdit { background: transparent; border: none; padding: 3px 2px; }"
    "QFrame#chip { background: #3a3a3a; border: 1px solid #454545; border-radius: 4px; }"
    "QFrame#chip:hover { background: #414141; }"
    "QFrame#chip QLabel { color: #ffffff; background: transparent; padding: 0; }"
    "QFrame#chip QToolButton { border: none; background: transparent; color: #c5c5c5; padding: 0 5px; }"
    "QFrame#chip QToolButton:hover { color: #ffffff; }"
)


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps onto new lines."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 4) -> None:
        super().__init__(parent)
        self._items: list = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def insertWidget(self, index: int, widget: QWidget) -> None:  # noqa: N802
        self.addChildWidget(widget)
        self._items.insert(index, QWidgetItem(widget))
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect: QRect, apply: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_height = area.x(), area.y(), 0
        space = self.spacing()
        for item in self._items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            if x + hint.width() > area.right() + 1 and line_height > 0:
                x, y = area.x(), y + line_height + space
                line_height = 0
            if apply:
                width = min(hint.width(), area.width())
                item.setGeometry(QRect(QPoint(x, y), QSize(width, hint.height())))
            x += hint.width() + space
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


class Chip(QFrame):
    removed = Signal(str)
    activated = Signal(str)

    def __init__(self, item_id: str, label: str, tooltip: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chip")
        self.item_id = item_id
        self.label = QLabel(label, self)
        self.close_button = QToolButton(self)
        self.close_button.setText("×")
        self.close_button.setToolTip(f"Remove {label}")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 2, 3, 2)
        layout.setSpacing(0)
        layout.addWidget(self.label)
        layout.addWidget(self.close_button)
        self.setToolTip(tooltip or label)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.close_button.clicked.connect(lambda: self.removed.emit(self.item_id))

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.activated.emit(self.item_id)
        super().mouseDoubleClickEvent(event)


class ChipInput(QWidget):
    changed = Signal()
    create_requested = Signal(str)  # text that matches no suggestion
    edit_requested = Signal(str)  # chip id

    def __init__(
        self,
        placeholder: str = "",
        single: bool = False,
        free_text: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.single = single
        self.free_text = free_text
        self._items: list[tuple[str, str]] = []
        self._suggestions: list[tuple[str, str, tuple[str, ...]]] = []  # id, label, other names
        self._chips: dict[str, Chip] = {}
        self.setObjectName("chipInput")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(palette.theme_qss(CHIP_STYLE))

        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setMinimumWidth(140)
        self.edit.setFrame(False)
        self._model = QStringListModel(self)
        self.completer = QCompleter(self._model, self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.edit.setCompleter(self.completer)

        self.flow = FlowLayout(self)
        self.flow.setContentsMargins(5, 4, 5, 4)
        self.flow.addWidget(self.edit)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        self.edit.returnPressed.connect(self._commit_text)
        self.completer.activated.connect(self._completer_chosen)

    # ------------------------------------------------------------ data
    def set_suggestions(self, suggestions) -> None:
        """``(id, label)`` or ``(id, label, aliases)`` items offered while typing."""
        cleaned = []
        for entry in suggestions:
            item_id, label, *rest = entry
            cleaned.append((item_id, label, tuple(rest[0]) if rest else ()))
        self._suggestions = cleaned
        self._model.setStringList([label for _, label, _ in cleaned])
        # Labels of existing chips may have been renamed.
        names = {i: label for i, label, _ in cleaned}
        if any(i in names and names[i] != label for i, label in self._items):
            self.set_items([(i, names.get(i, label)) for i, label in self._items], emit=False)

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)

    def ids(self) -> tuple[str, ...]:
        return tuple(i for i, _ in self._items)

    def labels(self) -> tuple[str, ...]:
        return tuple(label for _, label in self._items)

    def set_items(self, items, emit: bool = False) -> None:
        for chip in self._chips.values():
            self.flow.removeWidget(chip)
            chip.hide()
            chip.deleteLater()
        self._chips.clear()
        self._items = []
        for item_id, label in items:
            self._append(item_id, label)
        self._after_change(emit)

    def add_item(self, item_id: str, label: str) -> bool:
        if any(i == item_id or (self.free_text and lbl.casefold() == label.casefold()) for i, lbl in self._items):
            return False
        if self.single:
            for chip in self._chips.values():
                self.flow.removeWidget(chip)
                chip.hide()
                chip.deleteLater()
            self._chips.clear()
            self._items = []
        self._append(item_id, label)
        self._after_change(True)
        return True

    def remove_item(self, item_id: str) -> None:
        chip = self._chips.pop(item_id, None)
        if chip is not None:
            self.flow.removeWidget(chip)
            chip.hide()
            chip.deleteLater()
        self._items = [(i, lbl) for i, lbl in self._items if i != item_id]
        self._after_change(True)

    def clear(self) -> None:
        self.set_items([], emit=True)

    def chip(self, item_id: str) -> Chip | None:
        return self._chips.get(item_id)

    # --------------------------------------------------------- internals
    def _append(self, item_id: str, label: str) -> None:
        chip = Chip(item_id, label, parent=self)
        chip.removed.connect(self.remove_item)
        chip.activated.connect(self.edit_requested)
        self._chips[item_id] = chip
        self._items.append((item_id, label))
        self.flow.insertWidget(self.flow.count() - 1, chip)
        chip.show()

    def _after_change(self, emit: bool) -> None:
        self.edit.setPlaceholderText(self.edit.placeholderText())
        self.flow.invalidate()
        self.updateGeometry()
        if emit:
            self.changed.emit()

    def _match(self, text: str) -> tuple[str, str] | None:
        wanted = " ".join(text.split()).lstrip("@").casefold()
        for item_id, label, aliases in self._suggestions:
            if wanted in {x.lstrip("@").casefold() for x in (label, *aliases)}:
                return item_id, label
        return None

    def _completer_chosen(self, text: str) -> None:
        self.add_text(text)
        # QLineEdit writes the chosen text into the field itself, after this
        # slot runs, so the chip would be followed by the same text again.
        QTimer.singleShot(0, lambda: self._clear_if(text))

    def _clear_if(self, text: str) -> None:
        if self.edit.text() == text:
            self.edit.clear()

    def _commit_text(self) -> None:
        if self.completer.popup().isVisible():
            return  # the completer handles Enter itself
        self.add_text(self.edit.text())

    def add_text(self, text: str) -> None:
        """What happens when the user enters ``text`` (public for tests)."""
        parts = [text] if not self.free_text else text.replace(";", ",").split(",")
        for part in parts:
            part = " ".join(part.split()).lstrip("#").strip()
            if not part:
                continue
            match = self._match(part)
            if match is not None:
                self.add_item(*match)
            elif self.free_text:
                self.add_item(part, part)
            else:
                self.create_requested.emit(part)
        self.edit.clear()

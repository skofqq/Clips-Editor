"""Work steps, as pages in DaVinci Resolve: Edit → Data → Render → Publish.

The bar at the bottom of the window switches pages (Ctrl+1…4). Steps are not
locked: any page can be opened at any time. Each step shows a check mark when
it is done for the current edit, and "Next" moves on to the following step.
The Edit page is the editor itself (preview in the middle, docks around it);
the other pages fill the window and hide the editor's docks.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui.theme import icon
from ui.widgets.card import Card

EDIT, DATA, RENDER, PUBLISH = range(4)
PAGES = (
    # (title, icon, hint shown as a tooltip)
    ("Edit", "film", "Cut, lay out, subtitles, censor (Ctrl+1)"),
    ("Data", "document", "Title, description, tags (Ctrl+2)"),
    ("Render", "export", "Export the Short to a video file (Ctrl+3)"),
    ("Publish", "youtube", "Upload to YouTube (Ctrl+4)"),
)


class PageBar(QWidget):
    """The step switcher: four page buttons with a done mark, and Next."""

    page_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pageBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._done = [False] * len(PAGES)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: list[QToolButton] = []
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 4, 12, 4)
        row.setSpacing(4)
        row.addStretch(1)
        for index, (title, icon_name, hint) in enumerate(PAGES):
            button = QToolButton(self)
            button.setObjectName("pageButton")
            button.setCheckable(True)
            button.setIcon(icon(icon_name))
            button.setIconSize(QSize(18, 18))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setToolTip(hint)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.group.addButton(button, index)
            self.buttons.append(button)
            row.addWidget(button)
            if index + 1 < len(PAGES):
                arrow = QLabel("›", self)
                arrow.setObjectName("pageArrow")
                row.addWidget(arrow)
        row.addStretch(1)
        self.next_button = QPushButton(self)
        self.next_button.setObjectName("primary")
        row.addWidget(self.next_button)
        self.group.idClicked.connect(self.page_requested)
        self.next_button.clicked.connect(lambda: self.page_requested.emit(min(self.current() + 1, len(PAGES) - 1)))
        self.set_current(EDIT)
        self._update_texts()

    def current(self) -> int:
        return max(0, self.group.checkedId())

    def set_current(self, index: int) -> None:
        self.buttons[index].setChecked(True)
        last = index == len(PAGES) - 1
        self.next_button.setVisible(not last)
        if not last:
            self.next_button.setText(f"Next: {PAGES[index + 1][0]}  →")

    def set_done(self, index: int, done: bool) -> None:
        if self._done[index] != done:
            self._done[index] = done
            self._update_texts()

    def is_done(self, index: int) -> bool:
        return self._done[index]

    def _update_texts(self) -> None:
        for index, button in enumerate(self.buttons):
            mark = "  ✓" if self._done[index] else ""
            button.setText(f"{index + 1}  {PAGES[index][0]}{mark}")


def page_scroll(content: QWidget, parent: QWidget, max_width: int = 960) -> QScrollArea:
    """A full-page scroll area with ``content`` centred and at most ``max_width`` wide."""
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(24, 20, 24, 20)
    row.addStretch(1)
    content.setMaximumWidth(max_width)
    content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    row.addWidget(content, 100)
    row.addStretch(1)
    scroll = QScrollArea(parent)
    scroll.setObjectName("pageScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(holder)
    return scroll


class StepPage(QWidget):
    """A simple step page: a heading, a status card and one main action."""

    action_requested = Signal()

    def __init__(self, title: str, subtitle: str, action: str, icon_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)
        heading = QLabel(title, self)
        heading.setObjectName("pageTitle")
        column.addWidget(heading)
        self.card = Card(subtitle=subtitle, parent=self)
        self.status = QLabel(self.card)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.card.body.addWidget(self.status)
        self.action_button = QPushButton(action, self.card)
        self.action_button.setObjectName("primary")
        self.action_button.setIcon(icon(icon_name))
        row = QHBoxLayout()
        row.addWidget(self.action_button)
        row.addStretch(1)
        self.card.body.addLayout(row)
        column.addWidget(self.card)
        self.slot = QVBoxLayout()  # the page's own form (export settings, upload form)
        self.slot.setContentsMargins(0, 0, 0, 0)
        column.addLayout(self.slot)
        column.addStretch(1)
        self.content: QWidget | None = None
        self.action_button.clicked.connect(self.action_requested)

    def set_status(self, text: str, can_act: bool = True) -> None:
        self.status.setText(text)
        self.action_button.setEnabled(can_act)

    def set_content(self, widget: QWidget | None) -> None:
        """Put ``widget`` under the status card, replacing (and deleting) the previous one."""
        if self.content is widget:
            return
        if self.content is not None:
            self.slot.removeWidget(self.content)
            self.content.hide()
            self.content.deleteLater()
        self.content = widget
        if widget is not None:
            widget.setParent(self)
            self.slot.addWidget(widget)
            widget.show()

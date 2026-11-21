"""Work steps, as pages in DaVinci Resolve: Edit → Data → Render → Publish.

The bar at the bottom of the window switches pages (Ctrl+1…4). Steps are not
locked: any page can be opened at any time. Each step shows a check mark when
it is done for the current edit, and "Next" moves on to the following step.
The Edit page is the editor itself (preview in the middle, docks around it);
the other pages fill the window and hide the editor's docks.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui.theme import icon, palette
from ui.widgets.card import Card

ANIMATE = True  # page transitions and the sliding step indicator
PAGE_IN_MS = 280
INDICATOR_MS = 340

EDIT, DATA, RENDER, PUBLISH = range(4)
PAGES = (
    # (title, icon, hint shown as a tooltip)
    ("Edit", "film", "Cut, lay out, subtitles, censor (Ctrl+1)"),
    ("Data", "document", "Title, description, tags (Ctrl+2)"),
    ("Render", "render", "Export the Short to a video file (Ctrl+3)"),
    ("Publish", "export", "Upload to YouTube, TikTok and Instagram Reels (Ctrl+4)"),
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
        # The active step's pill slides from button to button (M3 Expressive spring).
        self.indicator = QFrame(self)
        self.indicator.setObjectName("pageIndicator")
        self.indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._indicator_anim = QPropertyAnimation(self.indicator, b"geometry", self)
        self._indicator_anim.setDuration(INDICATOR_MS)
        self._indicator_anim.setEasingCurve(QEasingCurve.Type.OutBack)
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

    def _indicator_rect(self) -> QRect:
        return self.buttons[self.current()].geometry()

    def _move_indicator(self, animate: bool) -> None:
        target = self._indicator_rect()
        if target.isEmpty():
            return
        self.indicator.lower()
        self.indicator.show()
        self._indicator_anim.stop()
        if animate and ANIMATE and self.isVisible() and not self.indicator.geometry().isEmpty():
            self._indicator_anim.setStartValue(self.indicator.geometry())
            self._indicator_anim.setEndValue(target)
            self._indicator_anim.start()
        else:
            self.indicator.setGeometry(target)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._move_indicator(False)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._move_indicator(False)

    def set_current(self, index: int) -> None:
        changed = index != self.current() or not self.buttons[index].isChecked()
        self.buttons[index].setChecked(True)
        self._move_indicator(changed)
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


def page_hero(step: int, title: str, subtitle: str, icon_name: str, parent: QWidget) -> Card:
    """The header of a step page: a tonal icon tile, "Step N of 4", a big title and a line
    of text, on a softly tinted surface (M3 Expressive hero)."""
    card = Card(parent=parent)
    card.setObjectName("pageHero")
    card.body.setContentsMargins(20, 18, 20, 18)
    card.body.setSpacing(10)
    tile = QLabel(card)
    tile.setObjectName("heroIcon")
    tile.setFixedSize(60, 60)
    tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
    tile.setPixmap(icon(icon_name, palette.accent().on_container).pixmap(30, 30))
    step_label = QLabel(f"STEP {step + 1} OF {len(PAGES)}", card)
    step_label.setObjectName("heroStep")
    heading = QLabel(title, card)
    heading.setObjectName("pageTitle")
    card.body.removeWidget(card.subtitle_label)
    card.subtitle_label.setText(subtitle)
    card.subtitle_label.setVisible(bool(subtitle))
    texts = QVBoxLayout()
    texts.setSpacing(0)
    texts.addWidget(step_label)
    texts.addWidget(heading)
    texts.addWidget(card.subtitle_label)
    row = QHBoxLayout()
    row.setSpacing(16)
    row.addWidget(tile, 0, Qt.AlignmentFlag.AlignTop)
    row.addLayout(texts, 1)
    card.body.insertLayout(0, row)
    card.heading = heading
    return card


def animate_in(widget: QWidget, distance: int = 22) -> None:
    """Fade a page in while it slides up a little (removed when done)."""
    if not ANIMATE or not widget.isVisible():
        return
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)
    fade = QPropertyAnimation(effect, b"opacity", widget)
    fade.setDuration(PAGE_IN_MS)
    fade.setStartValue(0.0)
    fade.setEndValue(1.0)
    fade.setEasingCurve(QEasingCurve.Type.OutCubic)
    fade.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def done() -> None:
        if widget.graphicsEffect() is effect:
            widget.setGraphicsEffect(None)

    fade.finished.connect(done)
    if distance <= 0:  # fade only (a widget the layout is still placing)
        return
    end = widget.pos()
    slide = QPropertyAnimation(widget, b"pos", widget)
    slide.setDuration(PAGE_IN_MS + 60)
    slide.setStartValue(end + QPoint(0, distance))
    slide.setEndValue(end)
    slide.setEasingCurve(QEasingCurve.Type.OutQuart)
    slide.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


class ServiceTabs(QWidget):
    """Tabs with a logo in front of each name; only the open tab takes room on the page."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bar = QTabBar(self)
        self.bar.setObjectName("serviceTabs")
        self.bar.setIconSize(QSize(20, 20))
        self.bar.setExpanding(False)
        self.bar.setDrawBase(False)
        self.bar.setDocumentMode(True)
        self.pages: list[QWidget] = []
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)
        column.addWidget(self.bar)
        self._column = column
        self.bar.currentChanged.connect(self._show)

    def add(self, page: QWidget, name: str, logo: str) -> int:
        page.setParent(self)
        self.pages.append(page)
        self._column.addWidget(page)
        index = self.bar.addTab(icon(logo), name)
        self._show(self.bar.currentIndex())
        return index

    def current(self) -> int:
        return self.bar.currentIndex()

    def set_current(self, index: int) -> None:
        self.bar.setCurrentIndex(index)

    def _show(self, index: int) -> None:
        for i, page in enumerate(self.pages):
            page.setVisible(i == index)
        if 0 <= index < len(self.pages):
            animate_in(self.pages[index], 0)


class StepPage(QWidget):
    """A simple step page: a hero header with the status and one main action."""

    action_requested = Signal()

    def __init__(self, title: str, subtitle: str, action: str, icon_name: str, parent: QWidget | None = None,
                 step: int = 0, hero_icon: str = ""):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)
        self.card = page_hero(step, title, subtitle, hero_icon or icon_name, self)
        self.status = QLabel(self.card)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.card.body.addWidget(self.status)
        self.action_button = QPushButton(action, self.card)
        self.action_button.setObjectName("primary")
        self.action_button.setIcon(icon(icon_name))
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        self.actions.addWidget(self.action_button)
        self.actions.addStretch(1)
        self.card.body.addLayout(self.actions)
        column.addWidget(self.card)
        self.slot = QVBoxLayout()  # the page's own form (export settings, upload form)
        self.slot.setContentsMargins(0, 0, 0, 0)
        column.addLayout(self.slot)
        self.footer = QVBoxLayout()  # more sections under the form (the render queue)
        self.footer.setContentsMargins(0, 0, 0, 0)
        column.addLayout(self.footer)
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

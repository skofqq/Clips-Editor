"""Help → Keyboard shortcuts (F1): every key and mouse gesture with what it does.

The keys are read from the window's actions, so the list always matches
what the app really does.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

# (group, action attribute on the main window, what it does)
ACTIONS = (
    ("Pages", "page_actions", ""),
    ("File", "act_new", "Start an empty project"),
    ("File", "act_open", "Open a saved project (.tssproj)"),
    ("File", "act_save", "Save the project"),
    ("File", "act_save_as", "Save the project under another name"),
    ("File", "act_import", "Add video files to the project"),
    ("File", "act_twitch", "Download a clip or VOD part from a Twitch link"),
    ("File", "act_export", "Go to Render: export the Short to a video file"),
    ("File", "act_upload", "Go to Publish: upload to YouTube, TikTok or Instagram Reels"),
    ("File", "act_exit", "Close the app"),
    ("Playback", "act_play", "Play or pause the preview"),
    ("Playback", "act_prev", "One frame back"),
    ("Playback", "act_next", "One frame forward"),
    ("Playback", "act_back_second", "One second back"),
    ("Playback", "act_forward_second", "One second forward"),
    ("Playback", "act_go_start", "Jump to the start of the clip"),
    ("Playback", "act_go_end", "Jump to the end of the clip"),
    ("Timeline", "act_zoom_in", "Zoom the timeline in"),
    ("Timeline", "act_zoom_out", "Zoom the timeline out"),
    ("Timeline", "act_zoom_fit", "Show the whole clip"),
    ("Timeline", "act_zoom_selection", "Zoom to the selected range"),
    ("Editing", "act_undo", "Undo the last change"),
    ("Editing", "act_redo", "Redo what was undone"),
    ("Editing", "act_split", "Cut the clip in two at the playhead"),
    ("Editing", "act_delete", "Delete the selected part, range, mute or blur box"),
    ("Editing", "act_trim", "Keep only the selected range"),
    ("Editing", "act_mute", "Mute (beep) the selected range"),
    ("Editing", "act_blur", "Add a blur box at the playhead or over the selected range"),
)
PAGE_TEXT = ("Edit page: cut, layout, subtitles, censor", "Data page: title, description, tags, cover",
             "Render page: export the video", "Publish page: YouTube, TikTok, Instagram Reels, History")
OTHER = (
    ("Help", "F1", "This list of shortcuts"),
    ("Censor list", "A", "Accept the selected word (it gets beeped)"),
    ("Censor list", "R", "Reject the selected word (it stays as it is)"),
    ("Censor list", "P", "Play the selected word"),
    ("Mouse on the timeline", "Wheel", "Scroll along the clip"),
    ("Mouse on the timeline", "Ctrl + Wheel", "Zoom in or out where the mouse is"),
    ("Mouse on the timeline", "Alt + Wheel", "Make the waveform taller or flatter"),
    ("Mouse on the timeline", "Drag on the ruler", "Select a range"),
    ("Mouse on the timeline", "Drag a box edge", "Change when a mute, blur box or part starts or ends"),
    ("Mouse on the preview", "Drag a region", "Move the webcam or game in the frame"),
    ("Mouse on the preview", "Drag the divider", "Change how much room the webcam and game get"),
    ("Mouse on the preview", "Double-click a region", "Reset its position and zoom"),
    ("Data page", "Double-click the Up / down slider", "Put the cover text back in its place"),
    ("Publish → History", "Double-click a Short", "Open the video in the browser"),
)


def key_text(sequences) -> str:
    texts = [s.toString(QKeySequence.SequenceFormat.NativeText) for s in sequences if not s.isEmpty()]
    return "  or  ".join(dict.fromkeys(t for t in texts if t))


def shortcut_rows(window) -> list[tuple[str, str, str]]:
    """(group, keys, what it does) for everything the window offers, in menu order."""
    rows: list[tuple[str, str, str]] = []
    for group, name, text in ACTIONS:
        if name == "page_actions":
            for action, page_text in zip(getattr(window, name, ()), PAGE_TEXT):
                rows.append((group, key_text(action.shortcuts()), page_text))
            continue
        action = getattr(window, name, None)
        if action is None:
            continue
        keys = key_text(action.shortcuts())
        if keys:
            rows.append((group, keys, text))
    rows += list(OTHER)
    return rows


class ShortcutsDialog(QDialog):
    def __init__(self, rows: list[tuple[str, str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.resize(780, 700)
        self.rows = rows
        title = QLabel("Keyboard shortcuts", self)
        title.setObjectName("pageTitle")
        hint = QLabel("Keys work anywhere in the window unless a text field has the focus. "
                      "Type to find a key or an action.", self)
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("Find: split, zoom, Ctrl+S…")
        self.search_edit.setClearButtonEnabled(True)
        self.tree = QTreeWidget(self)
        self.tree.setObjectName("shortcutTree")
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Keys", "What it does"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        close = QPushButton("Close", self)
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        column = QVBoxLayout(self)
        column.setContentsMargins(20, 18, 20, 18)
        column.setSpacing(10)
        column.addWidget(title)
        column.addWidget(hint)
        column.addWidget(self.search_edit)
        column.addWidget(self.tree, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close)
        column.addLayout(buttons)
        self.search_edit.textChanged.connect(self.fill)
        self.fill()
        self.search_edit.setFocus()

    def fill(self, text: str = "") -> None:
        needle = (text or self.search_edit.text()).strip().casefold()
        self.tree.clear()
        group_item = None
        current_group = None
        for group, keys, what in self.rows:
            if needle and needle not in f"{group} {keys} {what}".casefold():
                continue
            if group != current_group:
                group_item = QTreeWidgetItem([group, ""])
                group_item.setFirstColumnSpanned(True)
                font = group_item.font(0)
                font.setBold(True)
                group_item.setFont(0, font)
                group_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.tree.addTopLevelItem(group_item)
                group_item.setFirstColumnSpanned(True)
                current_group = group
            item = QTreeWidgetItem([keys, what])
            item.setToolTip(1, what)
            self.tree.addTopLevelItem(item)

    def visible_rows(self) -> list[tuple[str, str]]:
        """(keys, what) of the shown rows, group headers left out."""
        result = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.text(1):
                result.append((item.text(0), item.text(1)))
        return result

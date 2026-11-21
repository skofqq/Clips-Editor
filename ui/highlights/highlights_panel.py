"""Highlights panel: find the best moments (short ones, or Shorts-length
ranges in a long recording), review them, tick the ones to keep and save
them as separate clips. Nothing is cut until the user decides."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.highlights.finder import HIGH, LOW, NORMAL, Highlight
from core.highlights.moments import AUTO, LENGTHS_S
from core.timeline.timebase import format_timecode
from ui.widgets.card import Card

ROLE_INDEX = Qt.ItemDataRole.UserRole + 1
BEST, IN_ORDER = "best", "order"


def stars(score: float) -> str:
    filled = 1 + int(score >= 0.6) + int(score >= 0.85)
    return "★" * filled + "☆" * (3 - filled)


def _length(us: int) -> str:
    s = round(us / 1_000_000)
    return f"{s} s" if s < 60 else f"{s // 60}:{s % 60:02d}"


class HighlightsPanel(QWidget):
    find_requested = Signal(str)  # sensitivity
    play_requested = Signal(int)  # index into the highlights
    select_requested = Signal(int)
    keep_requested = Signal(int)
    accepted_changed = Signal(int, bool)  # index, ticked
    save_requested = Signal()  # save the ticked moments as clips
    chat_twitch_requested = Signal()
    chat_file_requested = Signal()
    chat_clear_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._highlights: list[Highlight] = []
        self._filling = False
        self._length_touched = False

        self.length_combo = QComboBox(self)
        self.length_combo.addItem("Short moments (a few seconds)", AUTO)
        for seconds in LENGTHS_S:
            self.length_combo.addItem(f"Shorts of about {seconds} s", seconds)
        self.length_combo.setToolTip("For a long clip or a stream recording pick a Short length: "
                                     "you get ready ranges with the setup and the reaction, best first")
        self.length_combo.activated.connect(lambda _i: setattr(self, "_length_touched", True))
        self.sensitivity_combo = QComboBox(self)
        self.sensitivity_combo.addItem("Fewer, strongest moments", LOW)
        self.sensitivity_combo.addItem("Normal", NORMAL)
        self.sensitivity_combo.addItem("More moments", HIGH)
        self.sensitivity_combo.setCurrentIndex(1)
        self.find_button = QPushButton("Find highlights", self)
        self.find_button.setObjectName("primary")
        self.find_button.setToolTip("Looks for loud moments (shouts, screams), chat bursts when the chat is "
                                    "loaded and, when the clip is transcribed, laughter, stretched words "
                                    "and hype words")
        self.status = QLabel(self)
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)

        search = Card("Find", "Marks the moments worth a Short on the timeline. Nothing is cut.", self)
        search.add_row("Length", self.length_combo)
        search.add_row("Show", self.sensitivity_combo)
        search.add_row(self.find_button)
        search.add_row(self.status)

        self.chat_label = QLabel(self)
        self.chat_label.setObjectName("muted")
        self.chat_label.setWordWrap(True)
        self.chat_twitch_button = QPushButton("From Twitch…", self)
        self.chat_twitch_button.setToolTip("Download the chat of the stream recording (twitch.tv/videos/…)")
        self.chat_file_button = QPushButton("Open file…", self)
        self.chat_file_button.setToolTip("A chat file from TwitchDownloader (JSON or TXT) or another chat log")
        self.chat_clear_button = QPushButton("Remove", self)
        chat_buttons = QHBoxLayout()
        for button in (self.chat_twitch_button, self.chat_file_button, self.chat_clear_button):
            chat_buttons.addWidget(button)
        chat = Card("Twitch chat", "Where the chat explodes, something happened.", self)
        chat.add_row(self.chat_label)
        chat.add_row(chat_buttons)
        self.chat_card = chat

        self.sort_combo = QComboBox(self)
        self.sort_combo.addItem("Best first", BEST)
        self.sort_combo.addItem("In clip order", IN_ORDER)
        self.list = QListWidget(self)
        self.list.setAlternatingRowColors(False)
        self.list.setMinimumHeight(160)
        self.list.setWordWrap(True)
        self.list.itemDoubleClicked.connect(lambda _item: self._emit(self.play_requested))
        self.list.itemChanged.connect(self._item_changed)
        self.play_button = QPushButton("Play", self)
        self.select_button = QPushButton("Select", self)
        self.select_button.setToolTip("Select the moment on the timeline (then T keeps it, M mutes it…)")
        self.keep_button = QPushButton("Keep only this", self)
        self.keep_button.setToolTip("Cut everything else away (undo with Ctrl+Z)")
        buttons = QHBoxLayout()
        for button in (self.play_button, self.select_button, self.keep_button):
            buttons.addWidget(button)
        self.ticked_label = QLabel(self)
        self.ticked_label.setObjectName("muted")
        self.save_button = QPushButton("Save ticked as clips…", self)
        self.save_button.setToolTip("Each ticked moment becomes its own video next to the recording "
                                    "(with its part of the transcript) and opens in Media. "
                                    "The recording itself is not changed.")
        save_row = QHBoxLayout()
        save_row.addWidget(self.ticked_label, 1)
        save_row.addWidget(self.save_button)
        found = Card("Moments", "Tick the moments you want as Shorts.", self)
        found.add_row("Sort", self.sort_combo)
        found.add_row(self.list)
        found.add_row(buttons)
        found.add_row(save_row)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(search)
        layout.addWidget(chat)
        layout.addWidget(found, 1)

        self.find_button.clicked.connect(lambda: self.find_requested.emit(self.sensitivity_combo.currentData()))
        self.play_button.clicked.connect(lambda: self._emit(self.play_requested))
        self.select_button.clicked.connect(lambda: self._emit(self.select_requested))
        self.keep_button.clicked.connect(lambda: self._emit(self.keep_requested))
        self.save_button.clicked.connect(self.save_requested)
        self.chat_twitch_button.clicked.connect(self.chat_twitch_requested)
        self.chat_file_button.clicked.connect(self.chat_file_requested)
        self.chat_clear_button.clicked.connect(self.chat_clear_requested)
        self.sort_combo.currentIndexChanged.connect(lambda _i: self.set_highlights(self._highlights))
        self.list.currentRowChanged.connect(lambda _row: self._update_buttons())
        self.set_chat_info(0)
        self.set_highlights([])

    # ------------------------------------------------------------ options
    @property
    def sensitivity(self) -> str:
        return self.sensitivity_combo.currentData()

    @property
    def length_s(self) -> int:
        return int(self.length_combo.currentData())

    def set_length(self, seconds: int) -> None:
        index = self.length_combo.findData(seconds)
        if index >= 0:
            self.length_combo.setCurrentIndex(index)

    def suggest_length(self, seconds: int) -> None:
        """A clip was opened: long clips default to Shorts-length moments
        (unless the user already chose a length)."""
        if not self._length_touched:
            self.set_length(seconds)

    # ------------------------------------------------------------ list
    def current_index(self) -> int:
        item = self.list.currentItem()
        return int(item.data(ROLE_INDEX)) if item is not None else -1

    def select_index(self, index: int) -> None:
        for row in range(self.list.count()):
            if self.list.item(row).data(ROLE_INDEX) == index:
                self.list.setCurrentRow(row)
                return

    def _emit(self, signal) -> None:
        index = self.current_index()
        if index >= 0:
            signal.emit(index)

    def _item_changed(self, item: QListWidgetItem) -> None:
        if self._filling:
            return
        index = int(item.data(ROLE_INDEX))
        ticked = item.checkState() == Qt.CheckState.Checked
        if 0 <= index < len(self._highlights) and self._highlights[index].accepted != ticked:
            from dataclasses import replace  # noqa: PLC0415

            self._highlights[index] = replace(self._highlights[index], accepted=ticked)
            self._update_ticked()
            self.accepted_changed.emit(index, ticked)

    def _update_buttons(self) -> None:
        has = self.current_index() >= 0
        for button in (self.play_button, self.select_button, self.keep_button):
            button.setEnabled(has)

    def _update_ticked(self) -> None:
        ticked = sum(1 for h in self._highlights if h.accepted)
        self.save_button.setEnabled(ticked > 0)
        if not self._highlights:
            self.ticked_label.setText("")
        elif ticked:
            total = sum(h.length_us for h in self._highlights if h.accepted)
            self.ticked_label.setText(f"{ticked} ticked · {_length(total)}")
        else:
            self.ticked_label.setText("Tick a moment to save it as a clip.")

    def set_busy(self, busy: bool, message: str = "") -> None:
        self.find_button.setEnabled(not busy)
        self.find_button.setText("Looking…" if busy else "Find highlights")
        if message:
            self.status.setText(message)

    def set_enabled_for(self, has_media: bool, has_transcript: bool) -> None:
        self.find_button.setEnabled(has_media)
        for button in (self.chat_twitch_button, self.chat_file_button):
            button.setEnabled(has_media)
        if not has_media:
            self.status.setText("Import a video first.")
        elif not has_transcript and not self._highlights:
            self.status.setText("Tip: transcribe the clip first to also find laughter and shouting in the words.")

    def set_chat_info(self, count: int, source: str = "") -> None:
        if count:
            text = f"{count:,} messages loaded".replace(",", " ")
            self.chat_label.setText(f"{text} · {source}" if source else text)
        else:
            self.chat_label.setText("No chat. Load the stream chat to find the moments viewers reacted to.")
        self.chat_clear_button.setEnabled(count > 0)

    def set_chat_busy(self, busy: bool, message: str = "") -> None:
        for button in (self.chat_twitch_button, self.chat_file_button):
            button.setEnabled(not busy)
        if busy:
            self.chat_clear_button.setEnabled(False)
        if message:
            self.chat_label.setText(message)

    def set_highlights(self, highlights) -> None:
        self._highlights = list(highlights or [])
        current = self.current_index()
        order = list(range(len(self._highlights)))
        if self.sort_combo.currentData() == BEST:
            order.sort(key=lambda i: -self._highlights[i].score)
        else:
            order.sort(key=lambda i: self._highlights[i].start_us)
        self._filling = True
        try:
            self.list.clear()
            for index in order:
                h = self._highlights[index]
                lines = [f"{format_timecode(h.start_us)} – {format_timecode(h.end_us)}  ·  {_length(h.length_us)}"
                         f"   {stars(h.score)}"]
                detail = h.label
                if h.chat_count:
                    detail += f"  ·  💬 {h.chat_count}"
                lines.append(detail)
                if h.quote:
                    lines.append(f"“{h.quote}”")
                item = QListWidgetItem("\n".join(lines))
                item.setData(ROLE_INDEX, index)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if h.accepted else Qt.CheckState.Unchecked)
                item.setToolTip(f"Strongest at {format_timecode(h.peak_us)}. Tick it to save it as a clip.")
                self.list.addItem(item)
        finally:
            self._filling = False
        if self._highlights:
            if current >= 0 and current < len(self._highlights):
                self.select_index(current)
            else:
                self.list.setCurrentRow(0)
        self._update_buttons()
        self._update_ticked()

    def highlights(self) -> list[Highlight]:
        return list(self._highlights)

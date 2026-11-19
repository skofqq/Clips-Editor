"""Transcript panel: run recognition, browse, seek and correct the text."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.timeline.timebase import format_timecode
from core.transcription.engine import (
    AUTO,
    DEFAULT_MODEL,
    MODEL_SIZES,
    VAD_LEVELS,
    TranscribeOptions,
    parse_vocabulary,
)
from core.transcription.model import LANGUAGES, Transcript
from ui.theme import palette
from ui.transcript.transcript_controller import TranscriptController
from ui.widgets.card import Card
from ui.widgets.empty_state import EmptyState

ROLE_KIND = Qt.ItemDataRole.UserRole + 1  # "segment" | "word"
ROLE_SEGMENT = Qt.ItemDataRole.UserRole + 2
ROLE_WORD = Qt.ItemDataRole.UserRole + 3
ROLE_START = Qt.ItemDataRole.UserRole + 4
COL_TIME, COL_TEXT = 0, 1

MODEL_HINTS = {
    "tiny": "fastest, lowest accuracy (75 MB)",
    "base": "fast (145 MB)",
    "small": "good balance (480 MB)",
    "medium": "accurate, slower (1.5 GB)",
    "large-v3": "most accurate, slow on CPU (3 GB)",
    "large-v3-turbo": "near large-v3 accuracy, faster (1.6 GB)",
}
CUSTOM_MODEL = "__custom__"
VAD_OFF = "off"
APPROX_COLOR = QColor(palette.TEXT_TERTIARY)


class TranscriptPanel(QWidget):
    transcribe_requested = Signal(object)  # TranscribeOptions
    cancel_requested = Signal()
    seek_requested = Signal(int)  # microseconds
    word_selected = Signal(int, int)  # start_us, end_us
    clear_requested = Signal()
    retranscribe_requested = Signal(object)  # TranscribeOptions, for the selected range
    learned_cleared = Signal()

    def __init__(self, controller: TranscriptController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._rebuilding = False
        self._following = False
        self._busy = False
        self._has_audio = False
        self._current_segment: int | None = None
        self._learned: list[str] = []
        self._range_available = False

        self.language_combo = QComboBox(self)
        self.language_combo.addItem("Auto-detect", AUTO)
        self.language_combo.addItem("Русский", "ru")
        self.language_combo.addItem("English", "en")
        self.model_combo = QComboBox(self)
        for size in MODEL_SIZES:
            self.model_combo.addItem(size, size)
        self.model_combo.addItem("Custom model…", CUSTOM_MODEL)
        self.model_combo.setCurrentIndex(self.model_combo.findData(DEFAULT_MODEL))
        self.custom_model_edit = QLineEdit(self)
        self.custom_model_edit.setPlaceholderText("Folder of a CTranslate2 Whisper model, or a Hugging Face id")
        self.custom_model_edit.setToolTip(
            "For example a Whisper model fine-tuned on Russian, converted for faster-whisper.\n"
            "A Hugging Face id is downloaded on first use."
        )
        self.device_combo = QComboBox(self)
        self.device_combo.addItem("Auto (GPU if available)", AUTO)
        self.device_combo.addItem("GPU (CUDA)", "cuda")
        self.device_combo.addItem("CPU", "cpu")

        self.vocabulary_edit = QLineEdit(self)
        self.vocabulary_edit.setPlaceholderText("Nicknames and special words, comma separated")
        self.vocabulary_edit.setToolTip(
            "Words Whisper should expect: streamer and guest nicknames, game terms, slang.\n"
            "Write them the way they should appear, e.g.  sasavot, rostikfacekid, хелин."
        )
        self.uncensored_check = QCheckBox("Write swear words as spoken (no self-censoring)", self)
        self.uncensored_check.setChecked(True)
        self.uncensored_check.setToolTip(
            "Primes the model with uncensored speech so profanity is transcribed literally,\n"
            "which the automatic censor (Phase 5) needs."
        )

        self.slang_check = QCheckBox("Expect gaming / streaming slang (катка, рофл, клатч, GG, KEKW…)", self)
        self.slang_check.setChecked(True)
        self.slang_check.setToolTip(
            "Primes the model with common gamer and Twitch slang so it is written as spoken\n"
            "instead of being 'corrected' into ordinary words. Add your own words above."
        )

        self.clean_check = QCheckBox("Clean up audio (cut rumble and hiss, lift quiet speech)", self)
        self.clean_check.setChecked(True)
        self.separate_check = QCheckBox("Separate the voice from game audio and music (slower)", self)
        self.separate_check.setToolTip(
            "Runs a voice separation model (MDX-Net, about 65 MB, downloaded once) before recognition.\n"
            "Helps when loud game sounds or music cover the speech. Runs on the CPU: about half the clip's length.")
        self.vad_combo = QComboBox(self)
        self.vad_combo.addItem("Speech detection: normal", "normal")
        self.vad_combo.addItem("Speech detection: sensitive (quiet speech)", "sensitive")
        self.vad_combo.addItem("Speech detection: off (whole audio)", VAD_OFF)
        self.vad_combo.setToolTip(
            "Skips parts without speech. 'Sensitive' keeps quiet or short phrases;\n"
            "'off' sends everything to the model (slower, more risk of invented text in silence)."
        )
        self.accurate_check = QCheckBox("Accurate (slower)", self)
        self.accurate_check.setToolTip("Searches more alternatives for every phrase.")
        self.learned_label = QLabel(self)
        self.learned_label.setObjectName("muted")
        self.learned_label.setWordWrap(True)
        self.learned_clear = QPushButton("Forget", self)
        self.learned_clear.setToolTip("Forget the words learned from your corrections")

        self.transcribe_button = QPushButton("Transcribe", self)
        self.transcribe_button.setObjectName("primary")
        self.cancel_button = QPushButton("Cancel", self)
        self.clear_button = QPushButton("Clear", self)
        self.retranscribe_button = QPushButton("Re-transcribe selection", self)
        self.retranscribe_button.setToolTip(
            "Recognise only the selected range (or the phrase under the playhead) again\n"
            "with the chosen model and the accurate search. Select a range on the timeline first."
        )
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.status_label = QLabel(self)
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        self.info_label = QLabel(self)
        self.info_label.setObjectName("muted")
        self.info_label.setWordWrap(True)

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Find in transcript…")
        self.search.setClearButtonEnabled(True)

        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Time", "Text"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setWordWrap(True)
        self.tree.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.tree.header().setSectionResizeMode(COL_TIME, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(True)

        options = QHBoxLayout()
        options.addWidget(self.language_combo, 1)
        options.addWidget(self.model_combo, 1)
        options.addWidget(self.device_combo, 1)
        buttons = QHBoxLayout()
        buttons.addWidget(self.transcribe_button, 1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.clear_button)

        # Recognition settings live in a card that folds away once there is
        # a transcript, so the text gets the room.
        self.settings_card = Card(parent=self)
        self.settings_toggle = QToolButton(self.settings_card)
        self.settings_toggle.setObjectName("cardToggle")
        self.settings_toggle.setText("Recognition settings")
        self.settings_toggle.setCheckable(True)
        self.settings_toggle.setChecked(True)
        self.settings_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.settings_toggle.setArrowType(Qt.ArrowType.DownArrow)
        self.settings_box = QWidget(self.settings_card)
        box = QVBoxLayout(self.settings_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        box.addLayout(options)
        box.addWidget(self.custom_model_edit)
        box.addWidget(self.vocabulary_edit)
        learned = QHBoxLayout()
        learned.addWidget(self.learned_label, 1)
        learned.addWidget(self.learned_clear)
        box.addLayout(learned)
        box.addWidget(self.uncensored_check)
        box.addWidget(self.slang_check)
        box.addWidget(self.clean_check)
        box.addWidget(self.separate_check)
        quality = QHBoxLayout()
        quality.addWidget(self.vad_combo, 1)
        quality.addWidget(self.accurate_check)
        box.addLayout(quality)
        self.settings_card.body.addWidget(self.settings_toggle)
        self.settings_card.body.addWidget(self.settings_box)
        self._settings_auto = True  # fold automatically until the user decides

        self.empty = EmptyState(
            "speech", "No transcript yet",
            "Press Transcribe to turn the speech into text. The words appear on the timeline "
            "and can be corrected here.", parent=self,
        )
        self.list_stack = QStackedWidget(self)
        self.list_stack.addWidget(self.empty)
        self.list_stack.addWidget(self.tree)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addWidget(self.settings_card)
        layout.addLayout(buttons)
        layout.addWidget(self.retranscribe_button)
        layout.addWidget(self.progress)
        layout.addWidget(self.status_label)
        layout.addWidget(self.info_label)
        layout.addWidget(self.search)
        layout.addWidget(self.list_stack, 1)
        self.settings_toggle.clicked.connect(self._on_settings_clicked)

        self.transcribe_button.clicked.connect(lambda: self.transcribe_requested.emit(self.options()))
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.clear_button.clicked.connect(self.clear_requested)
        self.retranscribe_button.clicked.connect(
            lambda: self.retranscribe_requested.emit(replace(self.options(), accurate=True))
        )
        self.learned_clear.clicked.connect(self.learned_cleared)
        self.model_combo.currentIndexChanged.connect(self._update_model_tip)
        self.model_combo.currentIndexChanged.connect(self._update_custom_model)
        self.custom_model_edit.textChanged.connect(lambda: self._update_buttons())
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.search.textChanged.connect(self._apply_filter)
        controller.changed.connect(self.rebuild)

        self._update_model_tip()
        self._update_custom_model()
        self.set_learned([])
        self.set_busy(False)
        self.rebuild()

    # ------------------------------------------------------------ options
    def model_name(self) -> str:
        data = self.model_combo.currentData()
        return self.custom_model_edit.text().strip() if data == CUSTOM_MODEL else data

    def options(self) -> TranscribeOptions:
        vocabulary = parse_vocabulary(self.vocabulary_edit.text())
        known = {v.casefold() for v in vocabulary}
        # The user's own list first: if the prompt is too long, learned words go first.
        vocabulary += tuple(w for w in self._learned if w.casefold() not in known)
        vad = self.vad_combo.currentData()
        return TranscribeOptions(
            model=self.model_name(),
            language=self.language_combo.currentData(),
            device=self.device_combo.currentData(),
            vocabulary=vocabulary,
            uncensored=self.uncensored_check.isChecked(),
            slang=self.slang_check.isChecked(),
            clean_audio=self.clean_check.isChecked(),
            separate_voice=self.separate_check.isChecked(),
            vad=vad != VAD_OFF,
            vad_threshold=VAD_LEVELS.get(vad, VAD_LEVELS["normal"]),
            accurate=self.accurate_check.isChecked(),
        )

    def set_quality_preferences(self, clean: bool, vad: str, accurate: bool, custom_model: str,
                                separate: bool = False) -> None:
        self.clean_check.setChecked(clean)
        self.separate_check.setChecked(separate)
        index = self.vad_combo.findData(vad)
        self.vad_combo.setCurrentIndex(max(0, index))
        self.accurate_check.setChecked(accurate)
        self.custom_model_edit.setText(custom_model)

    def quality_preferences(self) -> dict:
        return {
            "clean": self.clean_check.isChecked(),
            "separate": self.separate_check.isChecked(),
            "vad": self.vad_combo.currentData(),
            "accurate": self.accurate_check.isChecked(),
            "custom_model": self.custom_model_edit.text().strip(),
        }

    def set_learned(self, words: list[str]) -> None:
        self._learned = list(words)
        if words:
            shown = ", ".join(words[:8]) + ("…" if len(words) > 8 else "")
            self.learned_label.setText(f"Learned from your corrections ({len(words)}): {shown}")
        else:
            self.learned_label.setText("Words you correct in the transcript are remembered for next time.")
        self.learned_clear.setVisible(bool(words))

    def set_range_available(self, available: bool) -> None:
        self._range_available = available
        self._update_buttons()

    def _update_custom_model(self) -> None:
        self.custom_model_edit.setVisible(self.model_combo.currentData() == CUSTOM_MODEL)
        self._update_buttons()

    def set_preferences(
        self, model: str, language: str, vocabulary: str, uncensored: bool, slang: bool = True
    ) -> None:
        index = self.model_combo.findData(model)
        if index < 0 and ("/" in model or "\\" in model):
            index = self.model_combo.findData(CUSTOM_MODEL)
            self.custom_model_edit.setText(model)
        if index >= 0:
            self.model_combo.setCurrentIndex(index)
        index = self.language_combo.findData(language)
        if index >= 0:
            self.language_combo.setCurrentIndex(index)
        self.vocabulary_edit.setText(vocabulary)
        self.uncensored_check.setChecked(uncensored)
        self.slang_check.setChecked(slang)

    def set_downloaded_models(self, names: list[str]) -> None:
        for i in range(self.model_combo.count()):
            size = self.model_combo.itemData(i)
            if size == CUSTOM_MODEL:
                continue
            self.model_combo.setItemText(i, f"{size}  ✓" if size in names else size)
        self._update_model_tip()

    def _update_model_tip(self) -> None:
        size = self.model_combo.currentData()
        if size == CUSTOM_MODEL:
            self.model_combo.setToolTip("Your own faster-whisper (CTranslate2) model")
            return
        downloaded = "✓" in self.model_combo.currentText()
        note = "" if downloaded else " · downloads on first use"
        self.model_combo.setToolTip(f"{size}: {MODEL_HINTS.get(size, '')}{note}")

    # -------------------------------------------------------------- state
    def set_has_audio(self, has_audio: bool) -> None:
        self._has_audio = has_audio
        self.rebuild()

    def _update_buttons(self) -> None:
        busy = self._busy
        has_model = bool(self.model_name()) if hasattr(self, "custom_model_edit") else True
        self.transcribe_button.setEnabled(not busy and self._has_audio and has_model)
        if hasattr(self, "retranscribe_button"):
            self.retranscribe_button.setEnabled(
                not busy and has_model and self._range_available and self.controller.transcript is not None
            )
        self.cancel_button.setVisible(busy)
        self.clear_button.setEnabled(not busy and self.controller.transcript is not None)
        for combo in (self.language_combo, self.model_combo, self.device_combo, self.vocabulary_edit,
                      self.uncensored_check, self.slang_check, self.clean_check, self.vad_combo,
                      self.accurate_check, self.separate_check, self.custom_model_edit):
            combo.setEnabled(not busy)
        self.progress.setVisible(busy)

    def _show_settings(self, shown: bool) -> None:
        self.settings_box.setVisible(shown)
        self.settings_toggle.setChecked(shown)
        self.settings_toggle.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)

    def _on_settings_clicked(self, checked: bool) -> None:
        self._settings_auto = False
        self._show_settings(checked)

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self._update_buttons()
        if not busy:
            self.progress.setValue(0)
        if message or not busy:
            self.status_label.setText(message)
        self.status_label.setVisible(bool(self.status_label.text()))

    def set_progress(self, fraction: float, message: str) -> None:
        self.progress.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
        if message:
            self.status_label.setText(message)
            self.status_label.setVisible(True)

    # ---------------------------------------------------------------- list
    def rebuild(self) -> None:
        t = self.controller.transcript
        expanded = {
            self.tree.topLevelItem(i).data(0, ROLE_SEGMENT)
            for i in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(i).isExpanded()
        }
        self._rebuilding = True
        try:
            self.tree.clear()
            self._current_segment = None
            self.list_stack.setCurrentWidget(self.tree if t is not None else self.empty)
            self.search.setVisible(t is not None)
            if self._settings_auto:
                self._show_settings(t is None)
            if t is None:
                self.info_label.setText("" if self._has_audio else "This clip has no audio track.")
                self.info_label.setVisible(not self._has_audio)
                self.empty.set_text(
                    "No transcript yet" if self._has_audio else "No audio",
                    "Press Transcribe to turn the speech into text. The words appear on the timeline "
                    "and can be corrected here." if self._has_audio else "This clip has no audio track.",
                )
                return
            self.info_label.setVisible(True)
            self.info_label.setText(self._summary(t))
            for seg in t.segments:
                item = QTreeWidgetItem([format_timecode(seg.start_us), seg.text])
                item.setData(0, ROLE_KIND, "segment")
                item.setData(0, ROLE_SEGMENT, seg.id)
                item.setData(0, ROLE_START, seg.start_us)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                tips = [f"{format_timecode(seg.start_us)} – {format_timecode(seg.end_us)}"]
                if not seg.has_word_timing:
                    tips.append("Word timing is approximate for this segment.")
                    item.setText(COL_TIME, format_timecode(seg.start_us) + " ≈")
                if seg.edited:
                    tips.append("Edited by hand.")
                    font = item.font(COL_TEXT)
                    font.setItalic(True)
                    item.setFont(COL_TEXT, font)
                item.setToolTip(COL_TEXT, "\n".join(tips))
                for index, word in enumerate(seg.words):
                    child = QTreeWidgetItem([format_timecode(word.start_us), word.text])
                    child.setData(0, ROLE_KIND, "word")
                    child.setData(0, ROLE_SEGMENT, seg.id)
                    child.setData(0, ROLE_WORD, index)
                    child.setData(0, ROLE_START, word.start_us)
                    child.setFlags(child.flags() | Qt.ItemFlag.ItemIsEditable)
                    if word.exact:
                        child.setToolTip(
                            COL_TEXT,
                            f"{format_timecode(word.start_us)} – {format_timecode(word.end_us)}"
                            f" · confidence {word.probability:.0%}",
                        )
                    else:
                        child.setText(COL_TIME, "≈ " + format_timecode(word.start_us))
                        child.setToolTip(COL_TEXT, "No exact timing: only the segment span is known.")
                        for col in (COL_TIME, COL_TEXT):
                            child.setForeground(col, QBrush(APPROX_COLOR))
                    item.addChild(child)
                self.tree.addTopLevelItem(item)
                if seg.id in expanded:
                    item.setExpanded(True)
            self._apply_filter(self.search.text())
        finally:
            self._rebuilding = False
            self._update_buttons()

    @staticmethod
    def _summary(t: Transcript) -> str:
        language = LANGUAGES.get(t.language, t.language or "?")
        parts = [f"{language} ({t.language_probability:.0%})", f"model {t.model}", f"{len(t.segments)} segments"]
        words = t.words()
        if words:
            parts.append(f"{t.exact_word_ratio:.0%} of words have exact timing")
        return " · ".join(parts)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            item.setHidden(bool(needle) and needle not in item.text(COL_TEXT).casefold())

    def follow_playhead(self, us: int) -> None:
        """Highlight the segment under the playhead without seeking."""
        t = self.controller.transcript
        seg = t.segment_at(us) if t else None
        seg_id = seg.id if seg else None
        if seg_id == self._current_segment:
            return
        self._current_segment = seg_id
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            current = item.data(0, ROLE_SEGMENT) == seg_id
            font = item.font(COL_TIME)
            font.setBold(current)
            item.setFont(COL_TIME, font)
            item.setBackground(COL_TIME, QBrush(QColor(palette.accent().soft)) if current else QBrush())
            if current and self.tree.state() != QAbstractItemView.State.EditingState:
                self.tree.scrollToItem(item)

    # ------------------------------------------------------------- events
    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        start = item.data(0, ROLE_START)
        if start is None:
            return
        self.seek_requested.emit(int(start))
        if item.data(0, ROLE_KIND) == "word":
            t = self.controller.transcript
            seg = t.segment(item.data(0, ROLE_SEGMENT)) if t else None
            index = item.data(0, ROLE_WORD)
            if seg is not None and 0 <= index < len(seg.words) and seg.words[index].exact:
                word = seg.words[index]
                self.word_selected.emit(word.start_us, word.end_us)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._rebuilding or column != COL_TEXT:
            return
        seg_id = item.data(0, ROLE_SEGMENT)
        text = item.text(COL_TEXT)
        if item.data(0, ROLE_KIND) == "segment":
            changed = self.controller.edit_segment_text(seg_id, text)
        else:
            changed = self.controller.edit_word_text(seg_id, item.data(0, ROLE_WORD), text)
        if not changed:
            self.rebuild()  # restore the stored text (e.g. empty or invalid edit)


__all__ = ["TranscriptPanel", "QFont"]

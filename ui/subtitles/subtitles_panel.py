"""Subtitles tab: turn the transcript into captions on the Short.

Top to bottom: an on/off switch with style presets, the look (font, size,
colours), where and how many words, how censored words are written, and
the list of captions with split / merge and export to .srt.
The text itself is corrected in the Transcript tab; captions follow it.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFontComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.subtitles.model import (
    ANIM_NONE,
    ANIM_POP,
    ANIM_REVEAL,
    ANIM_SLIDE,
    ANIM_WORD,
    EMOJI_ABOVE,
    EMOJI_BELOW,
    EMOJI_INLINE,
    KARAOKE_FILL,
    KARAOKE_SWEEP,
    KARAOKE_WORD,
    MASK_ALL,
    MASK_FIRST,
    MASK_NONE,
    POSITION_DIVIDER,
    PRESETS,
    Caption,
    SubtitleEdits,
    SubtitleSettings,
    SubtitleStyle,
    style_from_preset,
)
from core.timeline.timebase import format_timecode
from ui.widgets.card import Card
from ui.widgets.empty_state import EmptyState

ROLE_START = Qt.ItemDataRole.UserRole + 1
ROLE_PRESET = Qt.ItemDataRole.UserRole + 2  # (built in?, name)
PREVIEW_SIZE = QSize(150, 64)
# Emoji offered for a caption, besides the automatic one.
EMOJI_CHOICES = ("😂", "🔥", "💀", "😱", "🏆", "💰", "❤️", "😡", "😭", "🤔", "👀", "🤡", "🐐", "🎉", "😎", "🙏",
                 "🤯", "🥶", "😈", "👑", "💯", "⚡", "🎯", "🫡")


class ColorButton(QToolButton):
    color_changed = Signal(str)

    def __init__(self, tip: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self._color = "#ffffff"
        self.setIconSize(QSize(28, 16))
        self.clicked.connect(self._choose)

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = color
        pixmap = QPixmap(28, 16)
        pixmap.fill(QColor(color[:7]))
        self.setIcon(QIcon(pixmap))

    def _choose(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._color[:7]), self, self.toolTip())
        if chosen.isValid():
            self.set_color(chosen.name())
            self.color_changed.emit(self._color)


class SubtitlesPanel(QWidget):
    settings_changed = Signal(object)  # SubtitleSettings
    seek_requested = Signal(int)  # output time (µs)
    split_requested = Signal()  # start a caption at the word under the playhead
    export_requested = Signal()
    user_presets_changed = Signal(dict)  # name → SubtitleStyle dict, to keep in the app settings

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = SubtitleSettings()
        self._captions: list[Caption] = []
        self._loading = False

        # ---- on/off and presets
        self.enabled_check = QCheckBox("Show subtitles on the video", self)
        self.enabled_check.setToolTip("Drawn on the preview and burnt into the exported video")
        self.preset_list = QListWidget(self)
        self.preset_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.preset_list.setIconSize(PREVIEW_SIZE)
        self.preset_list.setGridSize(QSize(PREVIEW_SIZE.width() + 12, PREVIEW_SIZE.height() + 30))
        self.preset_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.preset_list.setMovement(QListWidget.Movement.Static)
        self.preset_list.setWrapping(True)
        self.preset_list.setWordWrap(True)
        self.preset_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.preset_list.setMinimumHeight(2 * (PREVIEW_SIZE.height() + 30) + 8)
        self.preset_list.setToolTip("Click a style to apply it. Right-click your own style to delete it.")
        self.preset_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.save_preset_button = QPushButton("Save as my style…", self)
        self.save_preset_button.setToolTip("Keep the current look as a style of your own (stays for every project)")
        main = Card("Subtitles", "Built from the transcript. Correct words in the Transcript tab.", self)
        main.add_row(self.enabled_check)
        main.body.addWidget(self.preset_list)
        preset_row = QHBoxLayout()
        preset_row.addStretch(1)
        preset_row.addWidget(self.save_preset_button)
        main.body.addLayout(preset_row)

        # ---- look
        self.font_combo = QFontComboBox(self)
        self.size_spin = QSpinBox(self)
        self.size_spin.setRange(16, 200)
        self.size_spin.setSuffix(" px")
        self.bold_check = QCheckBox("Bold", self)
        self.upper_check = QCheckBox("CAPITALS", self)
        self.color_button = ColorButton("Text colour", self)
        self.highlight_button = ColorButton("Colour of the word being spoken", self)
        self.outline_button = ColorButton("Outline colour", self)
        self.outline_spin = QSpinBox(self)
        self.outline_spin.setRange(0, 30)
        self.outline_spin.setSuffix(" px")
        self.karaoke_check = QCheckBox("Highlight the spoken word", self)
        self.punct_check = QCheckBox("Hide commas and periods", self)
        self.punct_check.setToolTip("Shorts-style text without commas and full stops; ? and ! stay.")
        self.speakers_check = QCheckBox("Colour per speaker", self)
        self.speakers_check.setToolTip("After Find speakers (Transcript tab), every other speaker gets "
                                       "their own colour and a new caption starts when the voice changes.")
        self.animation_combo = QComboBox(self)
        for label, value in (("None", ANIM_NONE), ("Pop in", ANIM_POP), ("Pop in + word jump", ANIM_WORD),
                             ("Slide up and fade in", ANIM_SLIDE), ("Word by word", ANIM_REVEAL)):
            self.animation_combo.addItem(label, value)
        self.animation_combo.setToolTip("Pop in: each caption grows in as it appears.\n"
                                        "Word jump: the spoken word also jumps up and stays a bit bigger.\n"
                                        "Slide up: the caption rises into place and fades in.\n"
                                        "Word by word: each word pops in when it is spoken.")
        self.karaoke_combo = QComboBox(self)
        for label, value in (("Only the spoken word", KARAOKE_WORD), ("Spoken words stay coloured", KARAOKE_FILL),
                             ("Colour runs through the word (karaoke)", KARAOKE_SWEEP)):
            self.karaoke_combo.addItem(label, value)
        self.karaoke_combo.setToolTip("How the Spoken colour moves through the caption")
        self.dim_check = QCheckBox("Fade words not spoken yet", self)
        self.emphasis_check = QCheckBox("Colour key words", self)
        self.emphasis_check.setToolTip("Numbers and words like «жесть», «ахаха», «клатч», money… get their own colour")
        self.emphasis_button = ColorButton("Key word colour", self)
        self.emoji_place_combo = QComboBox(self)
        for label, value in (("Above", EMOJI_ABOVE), ("Below", EMOJI_BELOW), ("After the word", EMOJI_INLINE)):
            self.emoji_place_combo.addItem(label, value)
        self.emoji_place_combo.setToolTip("Where the emoji goes. After the word: it pops in when the word is spoken.")
        self.word_box_check = QCheckBox("Box on the spoken word", self)
        self.word_box_check.setToolTip("The spoken word sits on a rounded box in the Spoken colour.")
        self.emoji_check = QCheckBox("Emoji for key words", self)
        self.emoji_check.setToolTip("Laughter, «жесть», money, clutch… put a matching emoji above the caption.")
        self.shadow_check = QCheckBox("Shadow", self)
        self.box_check = QCheckBox("Box behind text", self)
        look = Card("Look", parent=self, label_width=84)
        look.add_row("Font", self.font_combo)
        look.add_row("Size", self.size_spin)
        text_row = QHBoxLayout()
        text_row.setSpacing(16)
        text_row.addWidget(self.bold_check)
        text_row.addWidget(self.upper_check)
        text_row.addStretch(1)
        look.add_row("Text", text_row)
        colors = QHBoxLayout()
        for label, button in (("Text", self.color_button), ("Spoken", self.highlight_button),
                              ("Outline", self.outline_button)):
            text = QLabel(label, self)
            text.setObjectName("muted")
            colors.addWidget(text)
            colors.addWidget(button)
            colors.addSpacing(8)
        colors.addStretch(1)
        look.add_row("Colours", colors)
        look.add_row("Outline", self.outline_spin)
        look.add_row(self.karaoke_check)
        look.add_row("Spoken", self.karaoke_combo)
        look.add_row(self.dim_check)
        look.add_row("Animation", self.animation_combo)
        look.add_row(self.word_box_check)
        emphasis_row = QHBoxLayout()
        emphasis_row.addWidget(self.emphasis_check)
        emphasis_row.addWidget(self.emphasis_button)
        emphasis_row.addStretch(1)
        look.add_row(emphasis_row)
        emoji_row = QHBoxLayout()
        emoji_row.addWidget(self.emoji_check)
        emoji_row.addWidget(self.emoji_place_combo)
        emoji_row.addStretch(1)
        look.add_row(emoji_row)
        look.add_row(self.speakers_check)
        look.add_row(self.punct_check)
        effects = QHBoxLayout()
        effects.setSpacing(16)
        effects.addWidget(self.shadow_check)
        effects.addWidget(self.box_check)
        effects.addStretch(1)
        look.add_row(effects)

        # ---- placement and grouping
        self.position_combo = QComboBox(self)
        self.position_combo.addItem("On the webcam / gameplay border", "divider")
        self.position_combo.addItem("Custom height", "custom")
        self.position_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.position_slider.setRange(5, 95)
        self.position_slider.setToolTip("Height of the subtitles: 5 % = top, 95 % = bottom")
        self.words_spin = QSpinBox(self)
        self.words_spin.setRange(1, 12)
        self.words_spin.setToolTip("Most words on screen at once")
        self.chars_spin = QSpinBox(self)
        self.chars_spin.setRange(4, 80)
        self.chars_spin.setToolTip("Most letters on one line")
        self.lines_spin = QSpinBox(self)
        self.lines_spin.setRange(1, 4)
        layout_card = Card("Placement", parent=self, label_width=84)
        layout_card.add_row("Where", self.position_combo)
        layout_card.add_row("Height", self.position_slider)
        layout_card.add_row("Words", self.words_spin)
        layout_card.add_row("Letters/line", self.chars_spin)
        layout_card.add_row("Lines", self.lines_spin)

        # ---- censored words
        self.mask_combo = QComboBox(self)
        self.mask_combo.addItem("First letter: б***", MASK_FIRST)
        self.mask_combo.addItem("Stars only: ****", MASK_ALL)
        self.mask_combo.addItem("Write as spoken", MASK_NONE)
        censor_card = Card("Muted words", "Words inside a mute region are written like this.", self)
        censor_card.add_row(self.mask_combo)

        # ---- captions
        self.list = QListWidget(self)
        self.list.setWordWrap(True)
        self.split_button = QPushButton("Split at playhead", self)
        self.split_button.setToolTip("Start a new caption at the word under the playhead")
        self.merge_button = QPushButton("Merge with next", self)
        self.merge_button.setToolTip("Join the selected caption and the next one")
        self.emoji_button = QPushButton("Emoji", self)
        self.emoji_button.setToolTip("Choose the emoji of the selected caption (or none)")
        self.emoji_menu = QMenu(self.emoji_button)
        self.emoji_button.setMenu(self.emoji_menu)
        self.reset_button = QPushButton("Reset", self)
        self.reset_button.setToolTip("Forget your splits, merges and chosen emoji")
        self.export_button = QPushButton("Save .srt…", self)
        self.export_button.setToolTip("Subtitle file for YouTube or another editor (times of the edited video)")
        buttons = QHBoxLayout()
        for button in (self.split_button, self.merge_button, self.emoji_button, self.reset_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.export_button)
        self.count_label = QLabel(self)
        self.count_label.setObjectName("muted")
        captions_card = Card("Captions", parent=self)
        captions_card.body.addWidget(self.count_label)
        captions_card.body.addWidget(self.list, 1)
        captions_card.body.addLayout(buttons)
        self.list.setMinimumHeight(160)

        self.empty = EmptyState("speech", "No transcript yet",
                                "Transcribe the clip in the Transcript tab: the subtitles are made from it.",
                                parent=self)
        content = QWidget(self)
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        for card in (main, look, layout_card, censor_card, captions_card):
            column.addWidget(card)
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.stack)

        # ---- wiring
        self.enabled_check.toggled.connect(self._edited)
        self._user_presets: dict[str, SubtitleStyle] = {}
        self.preset_list.itemClicked.connect(self._preset_clicked)
        self.preset_list.customContextMenuRequested.connect(self._preset_menu)
        self.save_preset_button.clicked.connect(self._save_preset)
        self._fill_emoji_menu()
        self.karaoke_combo.currentIndexChanged.connect(self._edited)
        self.emoji_place_combo.currentIndexChanged.connect(self._edited)
        self.emphasis_button.color_changed.connect(self._edited)
        self.font_combo.currentFontChanged.connect(self._edited)
        for spin in (self.size_spin, self.outline_spin, self.words_spin, self.chars_spin, self.lines_spin):
            spin.valueChanged.connect(self._edited)
        self.animation_combo.currentIndexChanged.connect(self._edited)
        for check in (self.bold_check, self.upper_check, self.karaoke_check, self.shadow_check, self.box_check,
                      self.speakers_check, self.punct_check, self.word_box_check, self.emoji_check,
                      self.dim_check, self.emphasis_check):
            check.toggled.connect(self._edited)
        for button in (self.color_button, self.highlight_button, self.outline_button):
            button.color_changed.connect(self._edited)
        self.position_combo.currentIndexChanged.connect(self._edited)
        self.position_slider.valueChanged.connect(self._edited)
        self.mask_combo.currentIndexChanged.connect(self._edited)
        self.list.itemClicked.connect(lambda item: self.seek_requested.emit(item.data(ROLE_START)))
        self.list.currentRowChanged.connect(lambda _row: self._update_buttons())
        self.split_button.clicked.connect(self.split_requested)
        self.merge_button.clicked.connect(self._merge_selected)
        self.reset_button.clicked.connect(self._reset_edits)
        self.export_button.clicked.connect(self.export_requested)
        self.set_settings(SubtitleSettings())
        self.set_has_transcript(False)
        self._fill_presets()

    # ------------------------------------------------------------ state
    def settings(self) -> SubtitleSettings:
        return self._settings

    def style(self) -> SubtitleStyle:
        return self._settings.style

    def set_settings(self, settings: SubtitleSettings) -> None:
        self._settings = settings
        s = settings.style
        self._loading = True
        try:
            self.enabled_check.setChecked(s.enabled)
            self.font_combo.setCurrentFont(QFont(s.font))
            self.size_spin.setValue(s.size)
            self.bold_check.setChecked(s.bold)
            self.upper_check.setChecked(s.uppercase)
            self.color_button.set_color(s.color)
            self.highlight_button.set_color(s.highlight)
            self.outline_button.set_color(s.outline)
            self.outline_spin.setValue(s.outline_px)
            self.karaoke_check.setChecked(s.karaoke)
            self.animation_combo.setCurrentIndex(max(0, self.animation_combo.findData(s.animation)))
            self.word_box_check.setChecked(s.highlight_box)
            self.emoji_check.setChecked(s.emoji)
            self.karaoke_combo.setCurrentIndex(max(0, self.karaoke_combo.findData(s.karaoke_mode)))
            self.dim_check.setChecked(s.dim_upcoming)
            self.emphasis_check.setChecked(s.emphasis)
            self.emphasis_button.set_color(s.emphasis_color)
            self.emoji_place_combo.setCurrentIndex(max(0, self.emoji_place_combo.findData(s.emoji_place)))
            self.speakers_check.setChecked(s.speaker_colors)
            self.punct_check.setChecked(s.hide_punctuation)
            self.shadow_check.setChecked(s.shadow)
            self.box_check.setChecked(s.box)
            divider = s.position == POSITION_DIVIDER
            self.position_combo.setCurrentIndex(0 if divider else 1)
            self.position_slider.setValue(72 if divider else int(round(s.position * 100)))
            self.words_spin.setValue(s.max_words)
            self.chars_spin.setValue(s.max_chars)
            self.lines_spin.setValue(s.max_lines)
            self.mask_combo.setCurrentIndex(max(0, self.mask_combo.findData(s.mask)))
        finally:
            self._loading = False
        self._update_enabled()

    def set_has_transcript(self, has: bool) -> None:
        self.stack.setCurrentIndex(1 if has else 0)

    def set_captions(self, captions: list[Caption]) -> None:
        self._captions = list(captions)
        row = self.list.currentRow()
        self.list.clear()
        for caption in captions:
            from core.subtitles.raster import caption_emoji

            emoji = caption_emoji(caption) if (self._settings.style.emoji or caption.emoji) else ""
            item = QListWidgetItem(f"{format_timecode(caption.start_us)}   {caption.text}{'  ' + emoji if emoji else ''}")
            item.setData(ROLE_START, caption.start_us)
            self.list.addItem(item)
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)
        self.count_label.setText(f"{len(captions)} caption{'s' if len(captions) != 1 else ''}"
                                 " · click one to jump to it")
        self._update_buttons()

    def captions(self) -> list[Caption]:
        return list(self._captions)

    def set_edits(self, edits: SubtitleEdits) -> None:
        self._settings = replace(self._settings, edits=edits)
        self._update_buttons()
        self.settings_changed.emit(self._settings)

    # ---------------------------------------------------------- handlers
    def _read_style(self) -> SubtitleStyle:
        custom = self.position_combo.currentData() == "custom"
        return replace(
            self._settings.style,
            enabled=self.enabled_check.isChecked(),
            font=self.font_combo.currentFont().family(),
            size=self.size_spin.value(),
            bold=self.bold_check.isChecked(),
            uppercase=self.upper_check.isChecked(),
            color=self.color_button.color(),
            highlight=self.highlight_button.color(),
            outline=self.outline_button.color(),
            outline_px=self.outline_spin.value(),
            karaoke=self.karaoke_check.isChecked(),
            animation=self.animation_combo.currentData(),
            highlight_box=self.word_box_check.isChecked(),
            emoji=self.emoji_check.isChecked(),
            karaoke_mode=self.karaoke_combo.currentData(),
            dim_upcoming=self.dim_check.isChecked(),
            emphasis=self.emphasis_check.isChecked(),
            emphasis_color=self.emphasis_button.color(),
            emoji_place=self.emoji_place_combo.currentData(),
            speaker_colors=self.speakers_check.isChecked(),
            hide_punctuation=self.punct_check.isChecked(),
            shadow=self.shadow_check.isChecked(),
            box=self.box_check.isChecked(),
            position=self.position_slider.value() / 100 if custom else POSITION_DIVIDER,
            max_words=self.words_spin.value(),
            max_chars=self.chars_spin.value(),
            max_lines=self.lines_spin.value(),
            mask=self.mask_combo.currentData(),
        ).cleaned()

    def _edited(self, *_args) -> None:
        self._update_enabled()
        if self._loading:
            return
        style = self._read_style()
        if style != self._settings.style:
            self._settings = replace(self._settings, style=style)
            self.settings_changed.emit(self._settings)

    # ---------------------------------------------------------- presets
    def set_user_presets(self, presets: dict) -> None:
        """The user's own styles (name → SubtitleStyle dict), from the app settings."""
        self._user_presets = {str(name): SubtitleStyle.from_dict(data) for name, data in (presets or {}).items()
                              if isinstance(data, dict)}
        self._fill_presets()

    def user_presets(self) -> dict[str, dict]:
        return {name: style.to_dict() for name, style in self._user_presets.items()}

    def _fill_presets(self) -> None:
        from core.subtitles.raster import style_preview

        self.preset_list.clear()
        entries = [(False, name, style) for name, style in self._user_presets.items()]
        entries += [(True, name, style) for name, style in PRESETS.items()]
        for built_in, name, style in entries:
            image = style_preview(style, PREVIEW_SIZE.width(), PREVIEW_SIZE.height())
            item = QListWidgetItem(QIcon(QPixmap.fromImage(image)), name if built_in else f"★ {name}")
            item.setData(ROLE_PRESET, (built_in, name))
            item.setToolTip(name if built_in else f"{name} — your style (right-click to delete)")
            self.preset_list.addItem(item)

    def preset_item(self, name: str, built_in: bool = True) -> QListWidgetItem | None:
        for row in range(self.preset_list.count()):
            item = self.preset_list.item(row)
            if item.data(ROLE_PRESET) == (built_in, name):
                return item
        return None

    def _preset_clicked(self, item: QListWidgetItem) -> None:
        built_in, name = item.data(ROLE_PRESET)
        preset = PRESETS.get(name) if built_in else self._user_presets.get(name)
        if preset is not None:
            self.apply_preset(preset)

    def apply_preset(self, preset: SubtitleStyle) -> None:
        # Keep where the subtitles sit and how muted words are written.
        style = style_from_preset(preset, self._settings.style)
        self.set_settings(replace(self._settings, style=style))
        self.settings_changed.emit(self._settings)

    def _save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Save style", "Name of your style:")
        name = name.strip()
        if ok and name:
            self.save_preset(name)

    def save_preset(self, name: str) -> None:
        self._user_presets[name] = replace(self._settings.style, enabled=True)
        self._fill_presets()
        self.user_presets_changed.emit(self.user_presets())

    def delete_preset(self, name: str) -> None:
        if self._user_presets.pop(name, None) is not None:
            self._fill_presets()
            self.user_presets_changed.emit(self.user_presets())

    def _preset_menu(self, pos: QPoint) -> None:
        item = self.preset_list.itemAt(pos)
        if item is None:
            return
        built_in, name = item.data(ROLE_PRESET)
        if built_in:
            return
        menu = QMenu(self)
        delete = menu.addAction(f"Delete «{name}»")
        if menu.exec(self.preset_list.viewport().mapToGlobal(pos)) is delete:
            self.delete_preset(name)

    # ------------------------------------------------------------ emoji
    def _fill_emoji_menu(self) -> None:
        self.emoji_menu.clear()
        auto = self.emoji_menu.addAction("Automatic (from the words)")
        auto.triggered.connect(lambda: self.set_caption_emoji(None))
        none = self.emoji_menu.addAction("No emoji")
        none.triggered.connect(lambda: self.set_caption_emoji(""))
        self.emoji_menu.addSeparator()
        for emoji in EMOJI_CHOICES:
            action = self.emoji_menu.addAction(emoji)
            action.triggered.connect(lambda _checked=False, e=emoji: self.set_caption_emoji(e))

    def set_caption_emoji(self, emoji: str | None, row: int | None = None) -> bool:
        """The emoji of caption ``row`` (the selected one by default); None = automatic, "" = none."""
        row = self.list.currentRow() if row is None else row
        if not 0 <= row < len(self._captions):
            return False
        first_word = self._captions[row].words[0].source_us
        self.set_edits(self._settings.edits.with_emoji(first_word, emoji))
        return True

    def _update_enabled(self) -> None:
        self.position_slider.setEnabled(self.position_combo.currentData() == "custom")
        self.outline_spin.setEnabled(not self.box_check.isChecked())
        self.highlight_button.setEnabled(self.karaoke_check.isChecked() or self.word_box_check.isChecked())
        self.karaoke_combo.setEnabled(self.karaoke_check.isChecked())
        self.emphasis_button.setEnabled(self.emphasis_check.isChecked())
        self.emoji_place_combo.setEnabled(self.emoji_check.isChecked())

    def _update_buttons(self) -> None:
        row = self.list.currentRow()
        self.merge_button.setEnabled(0 <= row < len(self._captions) - 1)
        self.reset_button.setEnabled(not self._settings.edits.empty)
        self.export_button.setEnabled(bool(self._captions))
        self.split_button.setEnabled(bool(self._captions))
        self.emoji_button.setEnabled(0 <= row < len(self._captions))

    def _merge_selected(self) -> None:
        row = self.list.currentRow()
        if not 0 <= row < len(self._captions) - 1:
            return
        first_word = self._captions[row + 1].words[0].source_us
        self.set_edits(self._settings.edits.join_at(first_word))

    def _reset_edits(self) -> None:
        self.set_edits(SubtitleEdits())

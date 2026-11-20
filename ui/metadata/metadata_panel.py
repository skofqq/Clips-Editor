"""Metadata tab: pick streamer, participants, game and stream type as chips,
choose a template, generate title / description / tags and edit them.

Manual edits are never overwritten silently: when you generate again and a
field was changed by hand, the panel asks whether to keep those edits.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.metadata.generate import (
    FIELDS,
    ClipMetadata,
    MetadataSelection,
    format_custom,
    generate,
    parse_custom,
)
from core.metadata.library import GAME, PARTICIPANT, STREAM_TYPE, STREAMER, TEMPLATE, Library, clean_tags
from core.youtube.metadata import MAX_DESCRIPTION_BYTES, MAX_TAGS_CHARS, MAX_TITLE, tags_length
from ui.chips.chip_input import ChipInput
from ui.theme import icon, palette

FIELD_NAMES = {"title": "title", "description": "description", "tags": "tags"}


class MetadataPanel(QWidget):
    metadata_changed = Signal(object)  # ClipMetadata
    library_requested = Signal(str, str)  # kind, entity id ("" = just open)

    def __init__(self, library: Library, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.library = library
        self._generated = None
        self._loading = False

        self.library_button = QPushButton("Library…", self)
        self.library_button.setIcon(icon("library"))
        self.library_button.setToolTip("Streamers, participants, games, stream types and templates")
        self.template_combo = QComboBox(self)
        self.streamer_chips = ChipInput("Streamer…", single=True, parent=self)
        self.participant_chips = ChipInput("Add participant…", parent=self)
        self.game_chips = ChipInput("Game…", single=True, parent=self)
        self.type_chips = ChipInput("Stream type…", parent=self)
        self.clip_title_edit = QLineEdit(self)
        self.clip_title_edit.setPlaceholderText("What happens in the clip")
        self.date_edit = QLineEdit(self)
        self.date_edit.setPlaceholderText("today")
        self.stream_url_edit = QLineEdit(self)
        self.stream_url_edit.setPlaceholderText("VOD link; the streamer's Twitch if empty")
        self.custom_edit = QPlainTextEdit(self)
        self.custom_edit.setPlaceholderText("Your variables, one per line:  promo = https://t.me/…")
        self.custom_edit.setFixedHeight(54)
        self.extra_tag_chips = ChipInput("Extra tags for this clip…", free_text=True, parent=self)
        self.generate_button = QPushButton("Generate metadata", self)
        self.generate_button.setIcon(icon("sparkle", palette.accent().on_fill))
        self.generate_button.setObjectName("primary")

        self.title_edit = QLineEdit(self)
        self.title_count = QLabel(self)
        self.description_edit = QPlainTextEdit(self)
        self.description_edit.setMinimumHeight(140)
        self.description_count = QLabel(self)
        self.tag_chips = ChipInput("Add tag…", free_text=True, parent=self)
        self.tags_count = QLabel(self)
        self.hashtags_label = QLabel(self)
        self.hashtags_label.setObjectName("muted")
        self.hashtags_label.setWordWrap(True)
        self.status_label = QLabel(self)
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        for label in (self.title_count, self.description_count, self.tags_count):
            label.setObjectName("muted")

        choose = QFormLayout()
        choose.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        template_row = QHBoxLayout()
        template_row.addWidget(self.template_combo, 1)
        template_row.addWidget(self.library_button)
        choose.addRow("Template", template_row)
        choose.addRow("Streamer", self.streamer_chips)
        choose.addRow("Participants", self.participant_chips)
        choose.addRow("Game", self.game_chips)
        choose.addRow("Stream type", self.type_chips)
        choose.addRow("Clip title", self.clip_title_edit)
        choose.addRow("Date", self.date_edit)
        choose.addRow("Stream URL", self.stream_url_edit)
        choose.addRow("Variables", self.custom_edit)
        choose.addRow("Extra tags", self.extra_tag_chips)

        result = QFormLayout()
        result.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        title_box = QVBoxLayout()
        title_box.addWidget(self.title_edit)
        title_box.addWidget(self.title_count, 0, Qt.AlignmentFlag.AlignRight)
        result.addRow("Title", title_box)
        description_box = QVBoxLayout()
        description_box.addWidget(self.description_edit, 1)
        description_box.addWidget(self.description_count, 0, Qt.AlignmentFlag.AlignRight)
        result.addRow("Description", description_box)
        tags_box = QVBoxLayout()
        tags_box.addWidget(self.tag_chips)
        tags_box.addWidget(self.tags_count, 0, Qt.AlignmentFlag.AlignRight)
        result.addRow("Tags", tags_box)

        layout = QVBoxLayout(self)
        layout.addLayout(choose)
        layout.addWidget(self.generate_button)
        layout.addWidget(self.status_label)
        layout.addSpacing(6)
        layout.addWidget(QLabel("<b>Result</b> (edit freely; used by Upload to YouTube)", self))
        layout.addLayout(result)
        layout.addWidget(self.hashtags_label)
        layout.addStretch(1)

        self.library_button.clicked.connect(lambda: self.library_requested.emit("", ""))
        self.generate_button.clicked.connect(self.generate_metadata)
        for chips, kind in ((self.streamer_chips, STREAMER), (self.participant_chips, PARTICIPANT),
                            (self.game_chips, GAME), (self.type_chips, STREAM_TYPE)):
            chips.changed.connect(self._emit)
            chips.create_requested.connect(lambda text, k=kind, c=chips: self._create(k, c, text))
            chips.edit_requested.connect(lambda entity_id, k=kind: self.library_requested.emit(
                self.library.person_kind(entity_id, k) if k in (STREAMER, PARTICIPANT) else k, entity_id))
        for chips in (self.extra_tag_chips, self.tag_chips):
            chips.changed.connect(self._emit)
        for edit in (self.clip_title_edit, self.date_edit, self.stream_url_edit, self.title_edit):
            edit.textChanged.connect(self._emit)
        for edit in (self.custom_edit, self.description_edit):
            edit.textChanged.connect(self._emit)
        self.template_combo.currentIndexChanged.connect(self._emit)
        self.refresh_library()
        self._update_counts()

    # ------------------------------------------------------------ library
    def refresh_library(self) -> None:
        """Reload suggestions and templates after the library changed."""
        self._loading = True
        keep = self.template_combo.currentData()
        self.template_combo.clear()
        self.template_combo.addItem("Automatic (profile or default template)", "")
        for template in self.library.all(TEMPLATE):
            self.template_combo.addItem(template.name + ("  ★" if template.is_default else ""), template.id)
        index = self.template_combo.findData(keep)
        self.template_combo.setCurrentIndex(max(0, index))
        for chips, kind in ((self.streamer_chips, STREAMER), (self.participant_chips, PARTICIPANT),
                            (self.game_chips, GAME), (self.type_chips, STREAM_TYPE)):
            # Streamers and participants are offered in both fields.
            entities = self.library.all_people(kind) if kind in (STREAMER, PARTICIPANT) else self.library.all(kind)
            chips.set_suggestions([(e.id, e.name, e.labels()) for e in entities])
            # Drop chips whose profile was deleted.
            known = {e.id for e in entities}
            if kind in (STREAMER, PARTICIPANT):
                known |= {e.id for e in self.library.all(STREAMER) + self.library.all(PARTICIPANT)}
            if any(i not in known for i in chips.ids()):
                chips.set_items([(i, label) for i, label in chips.items() if i in known])
        self._loading = False

    def _create(self, kind: str, chips: ChipInput, text: str) -> None:
        entity = self.library.create(kind, text, username=text.lstrip("@") if kind in (STREAMER, PARTICIPANT) else "")
        self.refresh_library()
        chips.add_item(entity.id, entity.name)
        self.status_label.setText(
            f"Added “{entity.name}” to the library. Double-click its chip to add links and tags."
        )

    # ------------------------------------------------------------ state
    def selection(self) -> MetadataSelection:
        return MetadataSelection(
            streamer_id=next(iter(self.streamer_chips.ids()), ""),
            participant_ids=self.participant_chips.ids(),
            game_id=next(iter(self.game_chips.ids()), ""),
            stream_type_ids=self.type_chips.ids(),
            template_id=self.template_combo.currentData() or "",
            clip_title=self.clip_title_edit.text().strip(),
            date=self.date_edit.text().strip(),
            stream_url=self.stream_url_edit.text().strip(),
            custom=tuple(parse_custom(self.custom_edit.toPlainText()).items()),
            project_tags=clean_tags(self.extra_tag_chips.labels()),
        )

    def metadata(self) -> ClipMetadata:
        return ClipMetadata(
            selection=self.selection(),
            generated=self._generated,
            title=self.title_edit.text(),
            description=self.description_edit.toPlainText(),
            tags=clean_tags(self.tag_chips.labels()),
        )

    def set_metadata(self, metadata: ClipMetadata | None) -> None:
        metadata = metadata or ClipMetadata()
        s = metadata.selection
        self._loading = True
        try:
            self.refresh_library()
            self._loading = True
            lib = self.library

            def chips_for(chips, kind, ids):
                found = lib.people(ids, kind) if kind in (STREAMER, PARTICIPANT) else lib.many(kind, ids)
                chips.set_items([(e.id, e.name) for e in found])

            chips_for(self.streamer_chips, STREAMER, [s.streamer_id] if s.streamer_id else [])
            chips_for(self.participant_chips, PARTICIPANT, s.participant_ids)
            chips_for(self.game_chips, GAME, [s.game_id] if s.game_id else [])
            chips_for(self.type_chips, STREAM_TYPE, s.stream_type_ids)
            index = self.template_combo.findData(s.template_id)
            self.template_combo.setCurrentIndex(max(0, index))
            self.clip_title_edit.setText(s.clip_title)
            self.date_edit.setText(s.date)
            self.stream_url_edit.setText(s.stream_url)
            self.custom_edit.setPlainText(format_custom(dict(s.custom)))
            self.extra_tag_chips.set_items([(t, t) for t in s.project_tags])
            self._generated = metadata.generated
            self.title_edit.setText(metadata.title)
            self.description_edit.setPlainText(metadata.description)
            self.tag_chips.set_items([(t, t) for t in metadata.tags])
            self.hashtags_label.setText(self._hashtag_text())
            self.status_label.setText("")
        finally:
            self._loading = False
        self._update_counts()

    def _hashtag_text(self) -> str:
        g = self._generated
        if g is None or not g.hashtags:
            return ""
        return "Hashtags in the description: " + " ".join(g.hashtags) + " (not sent as YouTube tags)"

    def _emit(self, *args) -> None:
        self._update_counts()
        if not self._loading:
            self.metadata_changed.emit(self.metadata())

    def _update_counts(self) -> None:
        current = self.metadata()
        edited = set(current.edited_fields()) if current.generated is not None else set()
        mark = " · edited by you"
        title_len = len(current.title.strip())
        self.title_count.setText(f"{title_len}/{MAX_TITLE}" + (mark if "title" in edited else ""))
        self.title_count.setStyleSheet(f"color: {palette.CRITICAL}" if title_len > MAX_TITLE else "")
        size = len(current.description.encode("utf-8"))
        self.description_count.setText(
            f"{size}/{MAX_DESCRIPTION_BYTES} bytes" + (mark if "description" in edited else "")
        )
        self.description_count.setStyleSheet(f"color: {palette.CRITICAL}" if size > MAX_DESCRIPTION_BYTES else "")
        length = tags_length(current.tags)
        self.tags_count.setText(
            f"{len(current.tags)} tags · {length}/{MAX_TAGS_CHARS} characters" + (mark if "tags" in edited else "")
        )
        self.tags_count.setStyleSheet(f"color: {palette.CRITICAL}" if length > MAX_TAGS_CHARS else "")

    # ------------------------------------------------------------ generate
    def ask_keep_edits(self, fields: list[str]) -> tuple[str, ...] | None:
        """Which edited fields to keep; None cancels. Separate so tests can answer."""
        names = ", ".join(FIELD_NAMES[f] for f in fields)
        box = QMessageBox(self)
        box.setWindowTitle("Generate metadata")
        box.setText(f"You changed the {names} by hand.")
        box.setInformativeText("Keep your edits and regenerate only the rest, or replace everything?")
        keep = box.addButton("Keep my edits", QMessageBox.ButtonRole.AcceptRole)
        replace_all = box.addButton("Replace all", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(keep)
        box.exec()
        clicked = box.clickedButton()
        if clicked is keep:
            return tuple(fields)
        if clicked is replace_all:
            return ()
        return None

    def generate_metadata(self) -> bool:
        current = self.metadata()
        result = generate(self.library, current.selection)
        edited = [f for f in current.edited_fields() if getattr(current, f) != getattr(result, f)]
        keep: tuple[str, ...] = ()
        if edited:
            answer = self.ask_keep_edits(edited)
            if answer is None:
                return False
            keep = answer
        updated = current.with_generated(result, keep)
        self.set_metadata(updated)
        notes = list(result.warnings)
        if keep:
            notes.insert(0, "Kept your edits: " + ", ".join(FIELD_NAMES[f] for f in keep) + ".")
        template = self.library.get(TEMPLATE, result.template_id)
        head = f"Generated with “{template.name}”." if template else "Generated."
        self.status_label.setText(" ".join([head, *notes]))
        self.metadata_changed.emit(self.metadata())
        return True

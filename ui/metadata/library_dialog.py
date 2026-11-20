"""Metadata library editor: streamers, participants, games, stream types, templates.

One tab per kind: a list on the left, the profile form on the right.
Changes are saved when you press Save, switch to another entry or close.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.metadata.generate import MetadataSelection, generate
from core.metadata.library import (
    GAME,
    PARTICIPANT,
    STREAM_TYPE,
    STREAMER,
    TEMPLATE,
    Library,
    LibraryError,
    blank,
    clean_tags,
    entity_from_dict,
    entity_to_dict,
    kind_label,
)
from core.metadata.template import BUILTIN_VARIABLES
from core.youtube.metadata import CATEGORIES
from ui.theme import palette

TAB_TITLES = {
    STREAMER: "Streamers", PARTICIPANT: "Participants", GAME: "Games", STREAM_TYPE: "Stream types",
    TEMPLATE: "Templates",
}
ROLE_ID = Qt.ItemDataRole.UserRole + 1

TEMPLATE_HELP = (
    "Variables: " + " ".join("{" + v + "}" for v in BUILTIN_VARIABLES)
    + " and your own (set them on the Data page as name = value).\n"
    "[[ … ]] marks an optional part: it is left out when a variable inside is empty. "
    "A line whose variables are all empty is left out too, so “Game: {game}” disappears without a game."
)


def _split(text: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in text.replace(";", ",").split(",") if p.strip())


def _parse_links(text: str) -> tuple[tuple[str, str], ...]:
    links = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        label, sep, url = line.partition("|")
        if not sep:
            label, url = "", line
        links.append((label.strip(), url.strip()))
    return tuple((label, url) for label, url in links if url)


def _format_links(links) -> str:
    return "\n".join(f"{label} | {url}" if label else url for label, url in links)


class EntityPage(QWidget):
    """List + form for one kind."""

    changed = Signal()

    def __init__(self, library: Library, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.library = library
        self.kind = kind
        self._current = None  # entity being edited (may be unsaved)
        self._loading = False
        self._dirty = False

        self.list = QListWidget(self)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.new_button = QPushButton(f"New {kind_label(kind).lower()}", self)
        self.delete_button = QPushButton("Delete", self)
        self.save_button = QPushButton("Save", self)
        self.save_button.setObjectName("primary")
        self.status = QLabel(self)
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)

        self.name_edit = QLineEdit(self)
        self.username_edit = QLineEdit(self)
        self.username_edit.setPlaceholderText("Twitch login, e.g. sasavot")
        self.youtube_edit = QLineEdit(self)
        self.youtube_edit.setPlaceholderText("https://www.youtube.com/@…")
        self.twitch_edit = QLineEdit(self)
        self.twitch_edit.setPlaceholderText("https://www.twitch.tv/…")
        self.links_edit = QPlainTextEdit(self)
        self.links_edit.setPlaceholderText("One per line: Telegram | https://t.me/…")
        self.links_edit.setFixedHeight(70)
        self.aliases_edit = QLineEdit(self)
        self.aliases_edit.setPlaceholderText("GTA 6, Grand Theft Auto VI")
        self.tags_edit = QLineEdit(self)
        self.tags_edit.setPlaceholderText("comma separated")
        self.template_combo = QComboBox(self)
        self.title_edit = QLineEdit(self)
        self.description_edit = QPlainTextEdit(self)
        self.description_edit.setMinimumHeight(150)
        self.hashtags_edit = QLineEdit(self)
        self.hashtags_edit.setPlaceholderText("shorts, twitch")
        self.category_combo = QComboBox(self)
        self.category_combo.addItem("Keep the upload dialog's choice", "")
        for cid, name in CATEGORIES.items():
            self.category_combo.addItem(name, cid)
        self.default_check = QCheckBox("Default template", self)
        self.help_label = QLabel(TEMPLATE_HELP, self)
        self.help_label.setObjectName("muted")
        self.help_label.setWordWrap(True)
        self.preview_label = QLabel(self)
        self.preview_label.setWordWrap(True)
        self.preview_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview_label.setStyleSheet(palette.theme_qss(
            "background: #1c1c1c; border: 1px solid #2f2f2f; padding: 8px; border-radius: 8px;"))

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("Name", self.name_edit)
        person = kind in (STREAMER, PARTICIPANT)
        if person:
            form.addRow("Username", self.username_edit)
            form.addRow("YouTube", self.youtube_edit)
            form.addRow("Twitch", self.twitch_edit)
            form.addRow("Other links", self.links_edit)
        if kind == GAME:
            form.addRow("Other names", self.aliases_edit)
        if kind == TEMPLATE:
            form.addRow("Title", self.title_edit)
            form.addRow("Description", self.description_edit)
            form.addRow("Hashtags", self.hashtags_edit)
            form.addRow("Category", self.category_combo)
            form.addRow("", self.default_check)
        form.addRow("YouTube tags", self.tags_edit)
        if kind in (STREAMER, GAME, STREAM_TYPE):
            form.addRow("Own template", self.template_combo)
        used = {form.itemAt(i).widget() for i in range(form.count())}
        for widget in (self.username_edit, self.youtube_edit, self.twitch_edit, self.links_edit,
                       self.aliases_edit, self.template_combo, self.title_edit, self.description_edit,
                       self.hashtags_edit, self.category_combo, self.default_check, self.help_label,
                       self.preview_label):
            if widget not in used and not (kind == TEMPLATE and widget in (self.help_label, self.preview_label)):
                widget.hide()

        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.list, 1)
        left_layout.addWidget(self.new_button)
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        right_layout.setContentsMargins(6, 0, 0, 0)
        right_layout.addLayout(form)
        if kind == TEMPLATE:
            right_layout.addWidget(self.help_label)
            right_layout.addWidget(QLabel("<b>Preview</b> (with sample values)", self))
            right_layout.addWidget(self.preview_label)
        right_layout.addStretch(1)
        scroll.setWidget(right)
        buttons = QHBoxLayout()
        buttons.addWidget(self.status, 1)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(self.save_button)
        right_box = QWidget(self)
        box_layout = QVBoxLayout(right_box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.addWidget(scroll, 1)
        box_layout.addLayout(buttons)
        splitter = QSplitter(self)
        splitter.addWidget(left)
        splitter.addWidget(right_box)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([200, 520])
        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

        self.list.currentItemChanged.connect(self._selection_changed)
        self.search.textChanged.connect(self._filter)
        self.new_button.clicked.connect(lambda: self.new_entry())
        self.save_button.clicked.connect(self.save_current)
        self.delete_button.clicked.connect(self.delete_current)
        for edit in (self.name_edit, self.username_edit, self.youtube_edit, self.twitch_edit, self.aliases_edit,
                     self.tags_edit, self.title_edit, self.hashtags_edit):
            edit.textEdited.connect(self._edited)
        for edit in (self.links_edit, self.description_edit):
            edit.textChanged.connect(self._edited)
        for combo in (self.template_combo, self.category_combo):
            combo.currentIndexChanged.connect(self._edited)
        self.default_check.toggled.connect(self._edited)
        self.reload()

    # ------------------------------------------------------------ list
    def reload(self, select_id: str | None = None) -> None:
        keep = select_id or (self._current.id if self._current is not None else None)
        self._loading = True
        self.template_combo.clear()
        self.template_combo.addItem("Default", "")
        for template in self.library.all(TEMPLATE):
            self.template_combo.addItem(template.name, template.id)
        self.list.clear()
        for entity in self.library.all(self.kind):
            label = entity.name + ("  ★" if getattr(entity, "is_default", False) else "")
            if getattr(entity, "username", "") and entity.username.casefold() != entity.name.casefold():
                label += f"  (@{entity.username})"
            item = QListWidgetItem(label)
            item.setData(ROLE_ID, entity.id)
            self.list.addItem(item)
        self._loading = False
        self._filter(self.search.text())
        if keep and self._select_id(keep):
            return
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._show(None)

    def _select_id(self, entity_id: str) -> bool:
        for i in range(self.list.count()):
            if self.list.item(i).data(ROLE_ID) == entity_id:
                self.list.setCurrentRow(i)
                self._show(self.library.get(self.kind, entity_id))
                return True
        return False

    def select(self, entity_id: str) -> bool:
        self._autosave()
        return self._select_id(entity_id)

    def _filter(self, text: str) -> None:
        wanted = text.strip().casefold()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(bool(wanted) and wanted not in item.text().casefold())

    def _selection_changed(self, current, previous) -> None:
        if self._loading:
            return
        self._autosave()
        entity_id = current.data(ROLE_ID) if current is not None else None
        self._show(self.library.get(self.kind, entity_id) if entity_id else None)

    # ------------------------------------------------------------ form
    def _show(self, entity) -> None:
        self._loading = True
        self._current = entity
        e = entity
        self.name_edit.setText(e.name if e else "")
        self.username_edit.setText(getattr(e, "username", ""))
        self.youtube_edit.setText(getattr(e, "youtube_url", ""))
        self.twitch_edit.setText(getattr(e, "twitch_url", ""))
        self.links_edit.setPlainText(_format_links(getattr(e, "social_links", ())))
        self.aliases_edit.setText(", ".join(getattr(e, "aliases", ())))
        self.tags_edit.setText(", ".join(getattr(e, "default_tags", ())))
        self.title_edit.setText(getattr(e, "title", ""))
        self.description_edit.setPlainText(getattr(e, "description", ""))
        self.hashtags_edit.setText(", ".join(getattr(e, "hashtags", ())))
        index = self.category_combo.findData(getattr(e, "category_id", ""))
        self.category_combo.setCurrentIndex(max(0, index))
        self.default_check.setChecked(bool(getattr(e, "is_default", False)))
        index = self.template_combo.findData(getattr(e, "template_id", ""))
        self.template_combo.setCurrentIndex(max(0, index))
        for widget in (self.name_edit, self.save_button, self.delete_button):
            widget.setEnabled(e is not None)
        self._loading = False
        self._dirty = False
        self.status.setText("")
        self._update_preview()

    def form_entity(self):
        """The entity as the form shows it now (not saved)."""
        if self._current is None:
            return None
        data = entity_to_dict(self._current)
        data.update(name=self.name_edit.text().strip(), default_tags=clean_tags(_split(self.tags_edit.text())))
        if self.kind in (STREAMER, PARTICIPANT):
            data.update(
                username=self.username_edit.text().strip().lstrip("@"),
                youtube_url=self.youtube_edit.text().strip(),
                twitch_url=self.twitch_edit.text().strip(),
                social_links=_parse_links(self.links_edit.toPlainText()),
            )
        if self.kind == GAME:
            data["aliases"] = _split(self.aliases_edit.text())
        if self.kind == TEMPLATE:
            data.update(
                title=self.title_edit.text(),
                description=self.description_edit.toPlainText(),
                hashtags=_split(self.hashtags_edit.text()),
                category_id=self.category_combo.currentData() or "",
                is_default=self.default_check.isChecked(),
            )
        if self.kind in (STREAMER, GAME, STREAM_TYPE):
            data["template_id"] = self.template_combo.currentData() or ""
        return entity_from_dict(self.kind, data)

    def _edited(self, *args) -> None:
        if self._loading or self._current is None:
            return
        self._dirty = True
        self.status.setText("Not saved yet.")
        self._update_preview()

    def _update_preview(self) -> None:
        if self.kind != TEMPLATE:
            return
        entity = self.form_entity()
        if entity is None:
            self.preview_label.setText("")
            return
        preview = Library(None)  # sample data, never touches the user's library
        try:
            preview.save(replace(entity, is_default=True, name="__preview__"))
            streamer = preview.create(STREAMER, "sasavot", username="sasavot",
                                      twitch_url="https://www.twitch.tv/sasavot")
            people = [preview.create(PARTICIPANT, n, twitch_url=f"https://www.twitch.tv/{n}")
                      for n in ("helin139", "iceicell")]
            game = preview.find(GAME, "GTA VI")
            result = generate(preview, MetadataSelection(
                streamer_id=streamer.id, participant_ids=tuple(p.id for p in people),
                game_id=game.id if game else "", stream_type_ids=("st-2",), template_id=entity.id,
                clip_title="Угнал танк у военных",
            ))
        finally:
            preview.close()
        warnings = ("\n⚠ " + "\n⚠ ".join(result.warnings)) if result.warnings else ""
        self.preview_label.setText(f"{result.title}\n\n{result.description}{warnings}")

    # ------------------------------------------------------------ actions
    def _autosave(self) -> None:
        if self._dirty and self._current is not None:
            self.save_current(quiet=True)

    def save_current(self, quiet: bool = False) -> bool:
        entity = self.form_entity()
        if entity is None:
            return False
        try:
            saved = self.library.save(entity)
        except LibraryError as exc:
            if quiet:
                self._dirty = False  # do not nag again; the entry simply stays unsaved
            else:
                QMessageBox.warning(self, kind_label(self.kind), str(exc))
            self.status.setText(f"Not saved: {exc}")
            return False
        self._dirty = False
        self._current = saved
        self.changed.emit()
        self.reload(saved.id)
        self.status.setText("Saved.")
        return True

    def new_entry(self, name: str = "") -> None:
        self._autosave()
        entity = blank(self.kind, name)
        self.list.clearSelection()
        self._show(entity)
        self._dirty = bool(name)
        self.name_edit.setFocus()
        self.status.setText("New entry: fill it in and press Save.")

    def delete_current(self) -> None:
        if self._current is None:
            return
        if self.library.get(self.kind, self._current.id) is None:
            self._show(None)
            return
        answer = QMessageBox.question(
            self, "Delete", f"Delete {kind_label(self.kind).lower()} “{self._current.name}” from the library?"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.library.delete(self.kind, self._current.id)
        self._current = None
        self._dirty = False
        self.changed.emit()
        self.reload()


class LibraryDialog(QDialog):
    library_changed = Signal()

    def __init__(self, library: Library, kind: str = STREAMER, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Metadata Library")
        self.resize(900, 720)
        self.library = library
        self.tabs = QTabWidget(self)
        self.pages: dict[str, EntityPage] = {}
        for k in (STREAMER, PARTICIPANT, GAME, STREAM_TYPE, TEMPLATE):
            page = EntityPage(library, k, self)
            page.changed.connect(self._changed)
            self.pages[k] = page
            self.tabs.addTab(page, TAB_TITLES[k])
        close = QPushButton("Close", self)
        close.clicked.connect(self.accept)
        note = QLabel("Saved on this computer and shared by all projects.", self)
        note.setObjectName("muted")
        bottom = QHBoxLayout()
        bottom.addWidget(note, 1)
        bottom.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(bottom)
        self.show_kind(kind)

    def _changed(self) -> None:
        for page in self.pages.values():
            if page is not self.sender() and not page._dirty:
                page.reload()
        self.library_changed.emit()

    def show_kind(self, kind: str) -> EntityPage:
        page = self.pages[kind]
        self.tabs.setCurrentWidget(page)
        return page

    def edit(self, kind: str, entity_id: str) -> None:
        self.show_kind(kind).select(entity_id)

    def create(self, kind: str, name: str) -> None:
        self.show_kind(kind).new_entry(name)

    def done(self, result: int) -> None:
        for page in self.pages.values():
            page._autosave()
        super().done(result)

"""Dialog for the user's own censor words and exclusions."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.censor.dictionary import Dictionary


def _lines(text: str) -> list[str]:
    seen: dict[str, str] = {}
    for line in text.splitlines():
        line = " ".join(line.split())
        if line and line.casefold() not in seen:
            seen[line.casefold()] = line
    return list(seen.values())


class DictionaryDialog(QDialog):
    def __init__(self, dictionary: Dictionary, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Censor dictionary")
        self.resize(560, 440)
        self.words_edit = QPlainTextEdit(self)
        self.words_edit.setPlainText("\n".join(dictionary.custom_words))
        self.words_edit.setPlaceholderText("кринж\nрофл*\n*вашу*")
        self.exclusions_edit = QPlainTextEdit(self)
        self.exclusions_edit.setPlainText("\n".join(dictionary.exclusions))
        self.exclusions_edit.setPlaceholderText("бляха\nхер")

        help_label = QLabel(
            "One word per line. <b>word</b> matches exactly, <b>word*</b> matches words starting with it, "
            "<b>*word*</b> matches words containing it. Spelling variants (ё/е, Latin look-alikes, "
            "stretched letters) are handled automatically.<br>"
            "Exclusions are never suggested, even when a built-in rule matches.",
            self,
        )
        help_label.setWordWrap(True)
        help_label.setObjectName("muted")

        columns = QHBoxLayout()
        for title, edit in (("Words to censor", self.words_edit), ("Never censor", self.exclusions_edit)):
            box = QVBoxLayout()
            box.addWidget(QLabel(title, self))
            box.addWidget(edit, 1)
            columns.addLayout(box, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(help_label)
        layout.addLayout(columns, 1)
        layout.addWidget(buttons)

    def result_dictionary(self) -> Dictionary:
        return Dictionary(_lines(self.words_edit.toPlainText()), _lines(self.exclusions_edit.toPlainText()))

"""Edit the replacement rules and the capitalisation option."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.transcription.cleanup import parse_rules

EXAMPLE = "скоф = Skof\nкс го -> CS:GO\nгг → GG"


class RulesDialog(QDialog):
    def __init__(self, rules: str, capitalise: bool, has_transcript: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Replacement Rules")
        self.setMinimumSize(460, 420)
        intro = QLabel(
            "Words the recogniser keeps getting wrong, one rule per line: <b>wrong = right</b>. "
            "Case, ё/е and punctuation are ignored when matching; a rule can match several words. "
            "Rules run after every transcription.", self)
        intro.setWordWrap(True)
        self.edit = QPlainTextEdit(rules, self)
        self.edit.setPlaceholderText(EXAMPLE)
        self.count_label = QLabel(self)
        self.count_label.setObjectName("muted")
        self.capitalise_check = QCheckBox("Start every sentence with a capital letter", self)
        self.capitalise_check.setChecked(capitalise)
        self.apply_check = QCheckBox("Apply to the current transcript now", self)
        self.apply_check.setChecked(has_transcript)
        self.apply_check.setEnabled(has_transcript)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
                                   self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.count_label)
        layout.addWidget(self.capitalise_check)
        layout.addWidget(self.apply_check)
        layout.addWidget(buttons)
        self.edit.textChanged.connect(self._count)
        self._count()

    def _count(self) -> None:
        n = len(parse_rules(self.rules()))
        self.count_label.setText(f"{n} rule{'s' if n != 1 else ''}")

    def rules(self) -> str:
        return self.edit.toPlainText().strip()

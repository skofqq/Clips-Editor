"""Fluent "card": a rounded, slightly raised group with a title.

Long forms are split into cards so the eye can find a section at a glance.
``card.form`` is a ready QFormLayout; ``card.body`` is the plain column it
sits in, for widgets that are not label/field rows.
"""

from __future__ import annotations

from PySide6.QtWidgets import QFormLayout, QFrame, QLabel, QVBoxLayout, QWidget


class Card(QFrame):
    def __init__(self, title: str = "", subtitle: str = "", parent: QWidget | None = None,
                 label_width: int = 0) -> None:
        super().__init__(parent)
        self._wanted = True  # what the app asks for (some cards come and go with the edit)
        self._user_hidden = False  # the user turned this block off (View → Blocks)
        self.label_width = label_width  # equal label columns across several cards
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(14, 12, 14, 14)
        self.body.setSpacing(8)
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("cardTitle")
        self.title_label.setVisible(bool(title))
        self.body.addWidget(self.title_label)
        self.subtitle_label = QLabel(subtitle, self)
        self.subtitle_label.setObjectName("muted")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(subtitle))
        self.body.addWidget(self.subtitle_label)
        self.form = QFormLayout()
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setHorizontalSpacing(12)
        self.form.setVerticalSpacing(8)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setLabelAlignment(self.form.labelAlignment())
        self.body.addLayout(self.form)

    def setVisible(self, visible: bool) -> None:  # noqa: N802 - Qt override
        self._wanted = bool(visible)
        super().setVisible(self._wanted and not self._user_hidden)

    def set_user_hidden(self, hidden: bool) -> None:
        """Hide the block whatever the app wants; showing it again restores the app's choice."""
        self._user_hidden = bool(hidden)
        super().setVisible(self._wanted and not self._user_hidden)

    @property
    def user_hidden(self) -> bool:
        return self._user_hidden

    def add_row(self, label, field=None) -> None:
        if field is None:
            self.form.addRow(label)
            return
        if isinstance(label, str):
            label = QLabel(label, self)
            if self.label_width:
                label.setMinimumWidth(self.label_width)
        self.form.addRow(label, field)

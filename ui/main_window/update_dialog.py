"""The "new version" window: what changed, download with progress, install."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout,
                               QWidget)

from app.config import APP_VERSION
from core.update.github import UpdateInfo
from ui.widgets.m3_progress import WavyProgressBar


class UpdateDialog(QDialog):
    download_requested = Signal()
    cancel_requested = Signal()
    install_requested = Signal()
    skip_requested = Signal()
    page_requested = Signal()
    auto_check_changed = Signal(bool)

    AVAILABLE, DOWNLOADING, READY, FAILED = range(4)

    def __init__(self, info: UpdateInfo, can_install: bool, auto_check: bool,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.info = info
        self.can_install = can_install
        self.state = self.AVAILABLE
        self.setWindowTitle("Update")
        self.setMinimumSize(480, 380)
        title = QLabel(f"Version {info.version} is available", self)
        title.setObjectName("pageTitle")
        size = f" · update {info.size_mb:.0f} MB" if info.size else ""
        self.subtitle = QLabel(f"You have {APP_VERSION}{size}. Settings, history and downloaded models stay "
                               "as they are.", self)
        self.subtitle.setObjectName("muted")
        self.subtitle.setWordWrap(True)
        self.notes = QTextBrowser(self)
        self.notes.setOpenExternalLinks(True)
        self.notes.setMarkdown(info.notes.strip() or "_No description for this release._")
        self.bar = WavyProgressBar(self)
        self.bar.setRange(0, 1000)
        self.bar.hide()
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.hide()
        self.auto_check = QCheckBox("Check for updates when the program starts", self)
        self.auto_check.setChecked(auto_check)
        self.auto_check.toggled.connect(self.auto_check_changed)

        self.skip_button = QPushButton("Skip this version", self)
        self.skip_button.clicked.connect(self._skip)
        self.page_button = QPushButton("Release page", self)
        self.page_button.setToolTip(info.page_url)
        self.page_button.clicked.connect(self.page_requested)
        self.later_button = QPushButton("Later", self)
        self.later_button.clicked.connect(self._later)
        self.main_button = QPushButton(self)
        self.main_button.setObjectName("primary")
        self.main_button.setDefault(True)
        self.main_button.clicked.connect(self._main)
        buttons = QHBoxLayout()
        buttons.addWidget(self.skip_button)
        buttons.addWidget(self.page_button)
        buttons.addStretch(1)
        buttons.addWidget(self.later_button)
        buttons.addWidget(self.main_button)

        column = QVBoxLayout(self)
        column.setContentsMargins(22, 18, 22, 16)
        column.setSpacing(8)
        column.addWidget(title)
        column.addWidget(self.subtitle)
        column.addWidget(self.notes, 1)
        column.addWidget(self.bar)
        column.addWidget(self.status)
        column.addWidget(self.auto_check)
        column.addLayout(buttons)
        self.set_state(self.AVAILABLE)

    # ------------------------------------------------------------ states
    def set_state(self, state: int, message: str = "") -> None:
        self.state = state
        self.bar.setVisible(state == self.DOWNLOADING)
        self.status.setVisible(bool(message) or state != self.AVAILABLE)
        self.status.setObjectName("error" if state == self.FAILED else "muted")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.skip_button.setVisible(state in (self.AVAILABLE, self.FAILED))
        self.later_button.setEnabled(state != self.DOWNLOADING)
        if not self.can_install:
            self.main_button.setText("Open download page")
            self.status.setText(message or "This copy runs from the source folder, so the new version is not "
                                "installed from here. Get it from the release page.")
            self.status.setVisible(True)
            self.page_button.hide()
            return
        if state == self.AVAILABLE:
            self.main_button.setText("Download and install")
            self.status.setText(message)
        elif state == self.DOWNLOADING:
            self.main_button.setText("Cancel")
            self.bar.setValue(0)
            self.status.setText(message or "Starting the download…")
        elif state == self.READY:
            self.main_button.setText("Install and restart")
            self.status.setText(message or "Downloaded. The program closes, installs the update and opens again.")
        else:
            self.main_button.setText("Try again")
            self.status.setText(message)

    def set_progress(self, fraction: float, message: str) -> None:
        self.bar.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
        if message:
            self.status.setText(message)

    # ------------------------------------------------------------ buttons
    def _main(self) -> None:
        if not self.can_install:
            self.page_requested.emit()
            self.accept()
        elif self.state == self.DOWNLOADING:
            self.cancel_requested.emit()
        elif self.state == self.READY:
            self.install_requested.emit()
        else:
            self.download_requested.emit()

    def _skip(self) -> None:
        self.skip_requested.emit()
        self.reject()

    def _later(self) -> None:
        self.reject()

    def reject(self) -> None:
        if self.state == self.DOWNLOADING:  # Esc / close while downloading = keep it going in the background
            self.hide()
            return
        super().reject()

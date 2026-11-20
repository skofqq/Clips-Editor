"""Side panel with the setup guide of a service (YouTube, TikTok, Instagram).

Opens on the right of the window from a "Guide" button or Help → Setup
guides, next to whatever page is open, so the steps stay visible while
you follow them. TikTok and Instagram also get their key / token fields here.
"""

from __future__ import annotations

import logging
import webbrowser
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.config import project_root
from core.social import instagram, tiktok
from core.workers import CancellableTask, Worker

log = logging.getLogger(__name__)

YOUTUBE, TIKTOK, INSTAGRAM = "youtube", "tiktok", "instagram"
GUIDES = {
    YOUTUBE: ("YouTube", "docs/YOUTUBE_SETUP_RU.md"),
    TIKTOK: ("TikTok", "docs/TIKTOK_SETUP_RU.md"),
    INSTAGRAM: ("Instagram Reels", "docs/INSTAGRAM_SETUP_RU.md"),
}
PANEL_WIDTH = 440


def _tiktok_connect(app, progress=None, cancel=None):
    token = tiktok.sign_in(app, webbrowser.open, cancel)
    try:
        name = tiktok.user_name(token.access_token)
    except Exception:  # noqa: BLE001 - the name is only shown
        name = ""
    return token, name


class TikTokSetup(QWidget):
    changed = Signal()

    def __init__(self, social, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.social = social
        self._task: CancellableTask | None = None
        self.key_edit = QLineEdit(self)
        self.key_edit.setPlaceholderText("Client key")
        self.secret_edit = QLineEdit(self)
        self.secret_edit.setPlaceholderText("Client secret")
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        redirect = QLineEdit(tiktok.REDIRECT_URI, self)
        redirect.setReadOnly(True)
        redirect.setToolTip("Add exactly this Redirect URI in Login Kit (Desktop)")
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.connect_button = QPushButton("Save and connect TikTok", self)
        self.connect_button.setObjectName("primary")
        self.disconnect_button = QPushButton("Disconnect", self)
        form = QFormLayout()
        form.addRow("Client key", self.key_edit)
        form.addRow("Client secret", self.secret_edit)
        form.addRow("Redirect URI", redirect)
        buttons = QHBoxLayout()
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        buttons.addStretch(1)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addLayout(form)
        column.addLayout(buttons)
        column.addWidget(self.status)
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self._disconnect)
        app = social.tiktok_app
        if app is not None:
            self.key_edit.setText(app.client_key)
            self.secret_edit.setText(app.client_secret)
        self.refresh()

    def refresh(self, message: str = "") -> None:
        name = self.social.tiktok_name
        busy = self._task is not None
        self.connect_button.setText("Cancel" if busy else ("Reconnect" if name else "Save and connect TikTok"))
        self.disconnect_button.setVisible(bool(name) and not busy)
        self.status.setText(message or (f"✓ Connected: {name}" if name else "Not connected."))

    def _connect(self) -> None:
        if self._task is not None:
            self._task.cancel()
            return
        key, secret = self.key_edit.text().strip(), self.secret_edit.text().strip()
        if not key or not secret:
            self.refresh("Paste the Client key and Client secret from developers.tiktok.com first.")
            return
        self.social.set_tiktok_app(key, secret)
        task = CancellableTask(_tiktok_connect, self.social.tiktok_app)
        task.signals.finished.connect(self._connected)
        task.signals.failed.connect(lambda exc: self._finish(f"Not connected: {exc}"))
        task.signals.cancelled.connect(lambda: self._finish("Cancelled."))
        self._task = task
        self.refresh("Finish the sign-in in the browser…")
        QThreadPool.globalInstance().start(task)

    def _connected(self, result) -> None:
        token, name = result
        self.social.tiktok_connected(token, name)
        self._finish("")
        self.changed.emit()

    def _finish(self, message: str) -> None:
        self._task = None
        if message.startswith("Not connected"):
            log.warning("TikTok connect: %s", message)
        self.refresh(message)

    def _disconnect(self) -> None:
        self.social.disconnect_tiktok()
        self.refresh()
        self.changed.emit()

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()


class InstagramSetup(QWidget):
    changed = Signal()

    def __init__(self, social, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.social = social
        self._worker: Worker | None = None
        self.token_edit = QLineEdit(self)
        self.token_edit.setPlaceholderText("Access token from the Meta dashboard")
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.connect_button = QPushButton("Connect Instagram", self)
        self.connect_button.setObjectName("primary")
        self.disconnect_button = QPushButton("Disconnect", self)
        form = QFormLayout()
        form.addRow("Access token", self.token_edit)
        buttons = QHBoxLayout()
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        buttons.addStretch(1)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addLayout(form)
        column.addLayout(buttons)
        column.addWidget(self.status)
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.refresh()

    def refresh(self, message: str = "") -> None:
        name = self.social.instagram_name
        self.disconnect_button.setVisible(bool(name))
        self.connect_button.setEnabled(self._worker is None)
        self.status.setText(message or (f"✓ Connected: {name}" if name else "Not connected."))

    def _connect(self) -> None:
        worker = Worker(instagram.connect, self.token_edit.text())
        worker.signals.finished.connect(self._connected)
        worker.signals.failed.connect(lambda exc: self._finish(f"Not connected: {exc}"))
        self._worker = worker
        self.refresh("Checking the token…")
        QThreadPool.globalInstance().start(worker)

    def _connected(self, account) -> None:
        self.social.instagram_connected(account)
        self.token_edit.clear()
        self._finish("")
        self.changed.emit()

    def _finish(self, message: str) -> None:
        self._worker = None
        self.refresh(message)

    def _disconnect(self) -> None:
        self.social.disconnect_instagram()
        self.refresh()
        self.changed.emit()


class GuidePanel(QFrame):
    """The side panel. ``show_service(name)`` opens it on a service's guide."""

    youtube_accounts_requested = Signal()
    connections_changed = Signal()

    def __init__(self, social, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("guidePanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(PANEL_WIDTH)
        self.service = ""
        self.title = QLabel(self)
        self.title.setObjectName("cardTitle")
        close = QToolButton(self)
        close.setText("✕")
        close.setToolTip("Close the guide")
        close.setAutoRaise(True)
        close.clicked.connect(self.hide)
        header = QHBoxLayout()
        header.addWidget(self.title, 1)
        header.addWidget(close)
        self.view = QTextBrowser(self)
        self.view.setOpenExternalLinks(True)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.document().setDefaultStyleSheet("a { color: #6cb8ff; }")
        self.setup = QStackedWidget(self)
        youtube = QWidget(self.setup)
        accounts = QPushButton("YouTube Accounts…", youtube)
        accounts.setObjectName("primary")
        accounts.clicked.connect(self.youtube_accounts_requested)
        row = QHBoxLayout(youtube)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(accounts)
        row.addStretch(1)
        self.tiktok_setup = TikTokSetup(social, self.setup)
        self.instagram_setup = InstagramSetup(social, self.setup)
        self._pages = {YOUTUBE: youtube, TIKTOK: self.tiktok_setup, INSTAGRAM: self.instagram_setup}
        for widget in self._pages.values():
            self.setup.addWidget(widget)
        for setup in (self.tiktok_setup, self.instagram_setup):
            setup.changed.connect(self.connections_changed)
        column = QVBoxLayout(self)
        column.setContentsMargins(16, 12, 16, 16)
        column.setSpacing(10)
        column.addLayout(header)
        column.addWidget(self.view, 1)
        column.addWidget(self.setup)
        self.hide()

    def show_service(self, service: str) -> None:
        title, guide = GUIDES[service]
        self.service = service
        self.title.setText(f"Set up {title}")
        try:
            self.view.setMarkdown(Path(project_root(), guide).read_text(encoding="utf-8"))
        except OSError:
            self.view.setPlainText(f"The guide was not found: {guide}")
        self.setup.setCurrentWidget(self._pages[service])
        for setup in (self.tiktok_setup, self.instagram_setup):
            setup.refresh()
        self.show()

    def toggle_service(self, service: str) -> None:
        if self.isVisible() and self.service == service:
            self.hide()
        else:
            self.show_service(service)


__all__ = ["GUIDES", "GuidePanel", "INSTAGRAM", "TIKTOK", "YOUTUBE"]

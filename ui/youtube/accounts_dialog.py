"""Settings → Integrations → YouTube: OAuth client, connected channels."""

from __future__ import annotations

import logging
import urllib.request
import webbrowser
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.config import project_root
from core.workers import CancellableTask, Worker
from core.youtube import oauth
from core.youtube.errors import YouTubeError
from core.youtube.oauth import ClientConfig
from core.youtube.service import YouTubeService

log = logging.getLogger(__name__)

SETUP_GUIDE = "docs/YOUTUBE_SETUP_RU.md"
ROLE_CHANNEL = Qt.ItemDataRole.UserRole + 1
_AVATARS: dict[str, bytes] = {}  # url -> image bytes, for this session


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310 - https avatar from the API
        return response.read(512 * 1024)


def _authorize(service: YouTubeService, progress=None, cancel=None):
    return service.authorize(webbrowser.open, cancel)


class YouTubeAccountsDialog(QDialog):
    accounts_changed = Signal()

    def __init__(self, service: YouTubeService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("YouTube Accounts")
        self.setMinimumWidth(560)
        self.service = service
        self._task: CancellableTask | None = None
        self._workers: set[Worker] = set()

        self.client_label = QLabel(self)
        self.client_label.setWordWrap(True)
        self.client_button = QPushButton("Load client_secret.json…", self)
        self.guide_button = QPushButton("How to get it", self)
        self.list = QListWidget(self)
        self.list.setIconSize(self.list.iconSize().expandedTo(QPixmap(32, 32).size()))
        self.connect_button = QPushButton("Connect…", self)
        self.connect_button.setObjectName("primary")
        self.reconnect_button = QPushButton("Reconnect", self)
        self.disconnect_button = QPushButton("Disconnect", self)
        self.active_button = QPushButton("Use for uploads", self)
        self.open_button = QPushButton("Open", self)
        self.cancel_button = QPushButton("Cancel sign-in", self)
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        note = QLabel(
            "You sign in on Google's own page in your browser; the app never sees your password. "
            "Tokens are kept in Windows Credential Manager, not in project files.\n"
            "Choose the channel (or brand account) on Google's screen. To use another channel, connect again "
            "and pick it there.\n"
            "Google keeps videos uploaded by an unverified API project private until the project passes "
            "YouTube's audit; such videos can be made public by hand in YouTube Studio.",
            self,
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        close = QPushButton("Close", self)

        client_row = QHBoxLayout()
        client_row.addWidget(self.client_label, 1)
        client_row.addWidget(self.client_button)
        client_row.addWidget(self.guide_button)
        buttons = QHBoxLayout()
        for b in (self.connect_button, self.reconnect_button, self.disconnect_button, self.active_button,
                  self.open_button):
            buttons.addWidget(b)
        bottom = QHBoxLayout()
        bottom.addWidget(self.cancel_button)
        bottom.addStretch(1)
        bottom.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>1. OAuth client</b> (Google Cloud → Credentials → Desktop app)", self))
        layout.addLayout(client_row)
        layout.addWidget(QLabel("<b>2. Channels</b>", self))
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.status_label)
        layout.addWidget(note)
        layout.addLayout(bottom)

        self.client_button.clicked.connect(self.load_client)
        self.guide_button.clicked.connect(self._open_guide)
        self.connect_button.clicked.connect(self.connect_account)
        self.reconnect_button.clicked.connect(self.connect_account)
        self.disconnect_button.clicked.connect(self.disconnect_account)
        self.active_button.clicked.connect(self._make_active)
        self.open_button.clicked.connect(self._open_channel)
        self.cancel_button.clicked.connect(self._cancel_sign_in)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        self.list.itemDoubleClicked.connect(lambda item: self._make_active())
        close.clicked.connect(self.reject)
        self.refresh()

    # ------------------------------------------------------------ state
    def selected_id(self) -> str | None:
        item = self.list.currentItem()
        return item.data(ROLE_CHANNEL) if item is not None and item.isSelected() else None

    def refresh(self) -> None:
        client = self.service.client
        if client is None:
            self.client_label.setText("Not set. Load the client_secret_….json file of a 'Desktop app' OAuth client.")
        else:
            self.client_label.setText(f"Loaded: {client.client_id[:24]}…")
        keep = self.selected_id()
        active = self.service.active
        self.list.clear()
        for channel in self.service.accounts:
            mark = "✓ " if active is not None and channel.id == active.id else ""
            handle = f" ({channel.custom_url})" if channel.custom_url else ""
            item = QListWidgetItem(f"{mark}{channel.title}{handle} — {self.service.status(channel.id)}")
            item.setData(ROLE_CHANNEL, channel.id)
            item.setToolTip(channel.url)
            self._set_avatar(item, channel.thumbnail_url)
            self.list.addItem(item)
            if channel.id == keep:
                item.setSelected(True)
                self.list.setCurrentItem(item)
        if not self.service.accounts:
            self.list.addItem(QListWidgetItem("No channel connected yet."))
            self.list.item(0).setFlags(Qt.ItemFlag.NoItemFlags)
        self._update_buttons()

    def _update_buttons(self) -> None:
        busy = self._task is not None
        selected = self.selected_id() is not None
        has_client = self.service.client is not None
        self.client_button.setEnabled(not busy)
        self.connect_button.setEnabled(not busy)  # without a client it asks for the file first
        self.reconnect_button.setEnabled(not busy and has_client and selected)
        for b in (self.disconnect_button, self.active_button, self.open_button):
            b.setEnabled(not busy and selected)
        self.cancel_button.setVisible(busy)

    def _set_avatar(self, item: QListWidgetItem, url: str) -> None:
        if not url.startswith(("https://", "http://")):
            return
        if url in _AVATARS:
            pixmap = QPixmap()
            if pixmap.loadFromData(_AVATARS[url]):
                item.setIcon(QIcon(pixmap))
            return
        worker = Worker(_fetch, url)
        worker.signals.finished.connect(lambda data, w=worker, u=url: self._avatar_loaded(w, u, data))
        worker.signals.failed.connect(lambda exc, w=worker: self._workers.discard(w))
        self._workers.add(worker)
        QThreadPool.globalInstance().start(worker)

    def _avatar_loaded(self, worker: Worker, url: str, data: bytes) -> None:
        self._workers.discard(worker)
        _AVATARS[url] = data
        self.refresh()

    # ------------------------------------------------------------ actions
    def load_client(self, path: str | None = None) -> bool:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "OAuth client file", "", "JSON (*.json)")
            if not path:
                return False
        try:
            client = ClientConfig.from_file(path)
        except YouTubeError as exc:
            QMessageBox.warning(self, "YouTube", str(exc))
            return False
        self.service.set_client(client)
        self.status_label.setText(
            "Client loaded. It is stored in Windows Credential Manager; you can delete the file."
            if self.service.store.persistent else "Client loaded for this session."
        )
        self.refresh()
        return True

    def connect_account(self) -> None:
        if self._task is not None:
            return
        if self.service.client is None and not self._ask_for_client():
            return
        task = CancellableTask(_authorize, self.service)
        task.signals.finished.connect(self._connected)
        task.signals.failed.connect(self._connect_failed)
        task.signals.cancelled.connect(self._connect_cancelled)
        self._task = task
        self.status_label.setText("Waiting for you to sign in and allow access in the browser…")
        self._update_buttons()
        QThreadPool.globalInstance().start(task)

    def _connected(self, result) -> None:
        self._task = None
        creds, channels = result
        channel = channels[0]
        self.service.add_account(creds, channel)
        self.status_label.setText(f"Connected: {channel.title}. Uploads go to this channel.")
        self.refresh()
        self.accounts_changed.emit()

    def _connect_failed(self, error) -> None:
        self._task = None
        message = str(error) if isinstance(error, YouTubeError) else f"Unexpected error: {error}"
        log.error("YouTube sign-in failed: %s", message)
        self.status_label.setText("Not connected.")
        self._update_buttons()
        QMessageBox.warning(self, "YouTube sign-in", message)

    def _connect_cancelled(self) -> None:
        self._task = None
        self.status_label.setText("Sign-in cancelled.")
        self._update_buttons()

    def _cancel_sign_in(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def disconnect_account(self) -> None:
        channel_id = self.selected_id()
        if channel_id is None:
            return
        name = next((c.title for c in self.service.accounts if c.id == channel_id), channel_id)
        answer = QMessageBox.question(
            self, "Disconnect", f"Disconnect {name}? The app's access is revoked and the saved sign-in deleted."
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        token = self.service.disconnect(channel_id)
        if token:
            worker = Worker(oauth.revoke, token)
            worker.signals.finished.connect(lambda _, w=worker: self._workers.discard(w))
            worker.signals.failed.connect(lambda _, w=worker: self._workers.discard(w))
            self._workers.add(worker)
            QThreadPool.globalInstance().start(worker)
        self.status_label.setText(f"{name} disconnected.")
        self.refresh()
        self.accounts_changed.emit()

    def _make_active(self) -> None:
        channel_id = self.selected_id()
        if channel_id is not None:
            self.service.set_active(channel_id)
            self.refresh()
            self.accounts_changed.emit()

    def _open_channel(self) -> None:
        channel_id = self.selected_id()
        channel = next((c for c in self.service.accounts if c.id == channel_id), None)
        if channel is not None:
            QDesktopServices.openUrl(QUrl(channel.url))

    def _ask_for_client(self) -> bool:
        """Connect was pressed before the OAuth client file was loaded."""
        box = QMessageBox(self)
        box.setWindowTitle("YouTube")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("First load your OAuth client file (client_secret_….json).")
        box.setInformativeText(
            "Google requires your own API client for uploads. Create it once in Google Cloud Console "
            "(type 'Desktop app'), download the JSON and load it here. 'How to get it' shows the steps."
        )
        load = box.addButton("Load client_secret.json…", QMessageBox.ButtonRole.AcceptRole)
        guide = box.addButton("How to get it", QMessageBox.ButtonRole.HelpRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is guide:
            self._open_guide()
            return False
        return box.clickedButton() is load and self.load_client()

    def _open_guide(self) -> None:
        """Show the setup guide inside the app (a .md file may have no program to open it on Windows)."""
        path = Path(project_root()) / SETUP_GUIDE
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            QMessageBox.warning(self, "YouTube", f"The guide was not found: {path}")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("How to connect YouTube")
        dialog.resize(720, 640)
        view = QTextBrowser(dialog)
        view.setOpenExternalLinks(True)
        view.setMarkdown(text)
        close = QPushButton("Close", dialog)
        close.clicked.connect(dialog.accept)
        layout = QVBoxLayout(dialog)
        layout.addWidget(view, 1)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self.guide_dialog = dialog
        dialog.show()

    def reject(self) -> None:
        if self._task is not None:
            self._task.cancel()
        super().reject()

"""File → Import from Twitch: paste a clip link, download it and fill in
the project's metadata (streamer, game, title, date)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.twitch.clip import ClipLink, TwitchClip, parse_clip_url
from core.twitch.download import download_clip, fetch_info
from core.workers import CancellableTask
from ui.widgets.m3_progress import WavyProgressBar


def default_folder() -> Path:
    return Path.home() / "Videos" / "Twitch Clips"


def _info_only(link: ClipLink, progress=None, cancel=None, ydl_factory=None):
    if progress:
        progress(0.2, "Reading clip information…")
    return None, fetch_info(link, ydl_factory)


class TwitchImportDialog(QDialog):
    """``result`` is ``(path or None, TwitchClip)`` after a successful run;
    the path is None in the "metadata only" mode."""

    def __init__(self, folder: str = "", autofill: bool = True, add_to_library: bool = True,
                 url: str = "", parent: QWidget | None = None, ydl_factory=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import from Twitch")
        self.setMinimumWidth(560)
        self.result: tuple[Path | None, TwitchClip] | None = None
        self._task: CancellableTask | None = None
        self._ydl_factory = ydl_factory

        layout = QVBoxLayout(self)
        intro = QLabel("Paste a link to a Twitch clip. It is downloaded in the best quality "
                       "and added to the project.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://clips.twitch.tv/…  or  https://www.twitch.tv/channel/clip/…")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.textChanged.connect(self._validate)
        layout.addWidget(self.url_edit)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Save to"))
        self.folder_edit = QLineEdit(folder or str(default_folder()))
        folder_row.addWidget(self.folder_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)

        self.autofill_check = QCheckBox("Fill in title, streamer, game and date in Metadata")
        self.autofill_check.setChecked(autofill)
        self.library_check = QCheckBox("Add new streamers and games to the Metadata Library")
        self.library_check.setChecked(add_to_library)
        self.autofill_check.toggled.connect(self.library_check.setEnabled)
        self.library_check.setEnabled(autofill)
        self.info_only_check = QCheckBox("The video is already in the project: only fill in metadata")
        self.info_only_check.toggled.connect(self._mode_changed)
        for box in (self.autofill_check, self.library_check, self.info_only_check):
            layout.addWidget(box)

        self.progress = WavyProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.buttons = QDialogButtonBox()
        self.import_button = self.buttons.addButton("Import", QDialogButtonBox.ButtonRole.AcceptRole)
        self.import_button.setDefault(True)
        self.cancel_button = self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.import_button.clicked.connect(self.start)
        self.cancel_button.clicked.connect(self._cancel)
        layout.addWidget(self.buttons)

        if not url:
            clip_text = QGuiApplication.clipboard().text() if QGuiApplication.instance() else ""
            url = clip_text if parse_clip_url(clip_text) else ""
        self.url_edit.setText(url)
        self._validate()

    # ------------------------------------------------------------- options
    @property
    def folder(self) -> str:
        return self.folder_edit.text().strip()

    @property
    def autofill(self) -> bool:
        return self.autofill_check.isChecked() or self.info_only_check.isChecked()

    @property
    def add_to_library(self) -> bool:
        return self.library_check.isChecked()

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Save Twitch clips to", self.folder)
        if folder:
            self.folder_edit.setText(folder)

    def _mode_changed(self, info_only: bool) -> None:
        self.autofill_check.setEnabled(not info_only)
        self.folder_edit.setEnabled(not info_only)
        self.import_button.setText("Fill in" if info_only else "Import")

    def _validate(self) -> bool:
        text = self.url_edit.text().strip()
        ok = parse_clip_url(text) is not None
        self.import_button.setEnabled(ok and self._task is None)
        if text and not ok:
            self.status.setText("This does not look like a Twitch clip link.")
        elif self._task is None:
            self.status.clear()
        return ok

    # ---------------------------------------------------------------- run
    def start(self) -> None:
        link = parse_clip_url(self.url_edit.text())
        if link is None or self._task is not None:
            return
        if self.info_only_check.isChecked():
            task = CancellableTask(_info_only, link, ydl_factory=self._ydl_factory)
        else:
            task = CancellableTask(download_clip, link, Path(self.folder), ydl_factory=self._ydl_factory)
        task.signals.progress.connect(self._progress)
        task.signals.finished.connect(self._done)
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(self._cancelled)
        self._task = task
        self._set_busy(True)
        QThreadPool.globalInstance().start(task)

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        self.progress.setValue(0)
        for widget in (self.url_edit, self.folder_edit, self.autofill_check, self.info_only_check):
            widget.setEnabled(not busy)
        self.import_button.setEnabled(not busy)
        if not busy:
            self._mode_changed(self.info_only_check.isChecked())
            self._validate()

    def _progress(self, fraction: float, message: str) -> None:
        self.progress.setValue(int(fraction * 1000))
        if message:
            self.status.setText(message)

    def _done(self, result) -> None:
        self._task = None
        self.result = result
        self._set_busy(False)
        self.accept()

    def _failed(self, error) -> None:
        self._task = None
        self._set_busy(False)
        self.status.setText(f"Import failed: {error}")

    def _cancelled(self) -> None:
        self._task = None
        self._set_busy(False)
        self.status.setText("Cancelled.")

    def _cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self.status.setText("Cancelling…")
        else:
            self.reject()

    def reject(self) -> None:
        if self._task is not None:
            self._task.cancel()
            return  # closes after the task reports back
        super().reject()

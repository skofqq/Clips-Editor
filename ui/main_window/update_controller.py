"""Help → Check for Updates: looks for a newer release on GitHub (also quietly
at start), downloads the update package and installs it after the program closes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QMessageBox, QToolButton

from app.config import APP_NAME, APP_VERSION
from core.update import github
from core.workers import CancellableTask, Worker

if TYPE_CHECKING:
    from ui.main_window.main_window import MainWindow
    from ui.main_window.update_dialog import UpdateDialog

log = logging.getLogger(__name__)


class UpdateController(QObject):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.config = window.config
        self.info: github.UpdateInfo | None = None
        self.downloaded: Path | None = None
        self.dialog: "UpdateDialog | None" = None
        self._checking = False
        self._worker: Worker | None = None  # kept so its signals live until delivered
        self._task: CancellableTask | None = None
        self.can_install = github.can_install_in_place()

        self.act_check = QAction("Check for Updates…", window)
        self.act_check.triggered.connect(lambda: self.check(manual=True))
        self.act_auto = QAction("Check for Updates at Start", window)
        self.act_auto.setCheckable(True)
        self.act_auto.setChecked(self.config.update_check)
        self.act_auto.toggled.connect(self.set_auto_check)
        # Shown in the status bar when a newer version is out.
        self.badge = QToolButton(window)
        self.badge.setObjectName("updateBadge")
        self.badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.badge.clicked.connect(self.show_dialog)
        self.badge.hide()
        window.statusBar().insertPermanentWidget(0, self.badge)

    @property
    def folder(self) -> Path:
        return github.downloads_dir(self.config.cache_dir)

    # ------------------------------------------------------------ checking
    def start(self) -> None:
        """At start: tidy old packages, then check quietly if allowed."""
        try:
            github.clean_downloads(self.folder)
        except OSError:
            pass
        if self.config.update_check:
            self.check(manual=False)

    def set_auto_check(self, on: bool) -> None:
        self.config.update_check = bool(on)
        if self.act_auto.isChecked() != on:
            self.act_auto.setChecked(on)
        self.config.save()

    def check(self, manual: bool = False) -> None:
        if self._checking:
            return
        if self.info is not None and manual:
            self.show_dialog()
            return
        self._checking = True
        if manual:
            self.window.statusBar().showMessage("Checking for updates…", 5000)
        worker = Worker(github.check_for_update, APP_VERSION)
        worker.signals.finished.connect(lambda info: self._checked(info, manual))
        worker.signals.failed.connect(lambda exc: self._check_failed(exc, manual))
        self._worker = worker
        self.window.pool.start(worker)

    def _checked(self, info: github.UpdateInfo | None, manual: bool) -> None:
        self._checking = False
        if info is None:
            if manual:
                QMessageBox.information(self.window, "Updates", f"{APP_NAME} {APP_VERSION} is the newest version.")
            return
        self.info = info
        self.badge.setText(f"Update to {info.version}")
        self.badge.setToolTip(f"{APP_NAME} {info.version} is available")
        self.badge.show()
        if manual or info.version != self.config.update_skipped:
            self.show_dialog()

    def _check_failed(self, exc: Exception, manual: bool) -> None:
        self._checking = False
        log.info("Update check failed: %s", exc)
        if manual:
            QMessageBox.warning(self.window, "Updates", f"Could not check for updates.\n\n{exc}")

    # ------------------------------------------------------------ dialog
    def show_dialog(self) -> None:
        if self.info is None:
            return
        from ui.main_window.update_dialog import UpdateDialog

        if self.dialog is None or self.dialog.info != self.info:
            dialog = UpdateDialog(self.info, self.can_install, self.config.update_check, self.window)
            dialog.download_requested.connect(self.download)
            dialog.cancel_requested.connect(self.cancel)
            dialog.install_requested.connect(self.install)
            dialog.skip_requested.connect(self.skip)
            dialog.page_requested.connect(self.open_page)
            dialog.auto_check_changed.connect(self.set_auto_check)
            self.dialog = dialog
            if self.downloaded is not None:
                dialog.set_state(dialog.READY)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def skip(self) -> None:
        if self.info is not None:
            self.config.update_skipped = self.info.version
            self.config.save()
            self.badge.hide()

    def open_page(self) -> None:
        url = self.info.page_url if self.info is not None else github.RELEASES_PAGE
        QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------ download / install
    @property
    def busy(self) -> bool:
        return self._task is not None

    def download(self) -> None:
        if self.info is None or self._task is not None:
            return
        task = CancellableTask(github.download_update, self.info, self.folder)
        task.signals.progress.connect(self._progress)
        task.signals.finished.connect(self._downloaded)
        task.signals.failed.connect(self._download_failed)
        task.signals.cancelled.connect(self._download_cancelled)
        self._task = task
        if self.dialog is not None:
            self.dialog.set_state(self.dialog.DOWNLOADING)
        self.badge.setText(f"Downloading {self.info.version}…")
        self.window.pool.start(task)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _progress(self, fraction: float, message: str) -> None:
        if self.dialog is not None:
            self.dialog.set_progress(fraction, message)
        if self.info is not None:
            self.badge.setText(f"Downloading {self.info.version}… {fraction * 100:.0f}%")

    def _downloaded(self, path: Path) -> None:
        self._task = None
        self.downloaded = Path(path)
        self.badge.setText(f"Install {self.info.version if self.info else ''}".strip())
        if self.dialog is not None:
            self.dialog.set_state(self.dialog.READY)
            self.show_dialog()

    def _download_failed(self, exc: Exception) -> None:
        self._task = None
        log.warning("Update download failed: %s", exc)
        self.badge.setText(f"Update to {self.info.version}" if self.info else "Update")
        if self.dialog is not None:
            self.dialog.set_state(self.dialog.FAILED, str(exc))
            self.show_dialog()

    def _download_cancelled(self) -> None:
        self._task = None
        self.badge.setText(f"Update to {self.info.version}" if self.info else "Update")
        if self.dialog is not None:
            self.dialog.set_state(self.dialog.AVAILABLE, "Download cancelled.")

    def install(self) -> None:
        """Close the program (asking about unsaved work as usual); the
        installer is started once it has closed — see app.bootstrap.run."""
        if self.downloaded is None or not self.downloaded.is_file():
            self.downloaded = None
            self.download()
            return
        self.window.update_installer = self.downloaded
        if self.dialog is not None:
            self.dialog.hide()
        if not self.window.close():
            self.window.update_installer = None  # the user chose to stay
            if self.dialog is not None:
                self.dialog.show()

    def shutdown(self) -> None:
        self.cancel()


def run_installer(window: "MainWindow") -> bool:
    """Called after the main window closed: start the downloaded update."""
    path = getattr(window, "update_installer", None)
    if not path:
        return False
    log_file = Path(window.config.log_dir) / "update-install.log"
    return github.start_installer(Path(path), log_file)

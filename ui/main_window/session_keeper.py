"""Autosave and undo history that survive a restart.

The main window owns one :class:`History` per clip (so switching clips no
longer wipes Ctrl+Z); this helper keeps them, writes crash-recovery copies
in the background while there are unsaved changes, stores the history of a
saved project on every save and offers to restore unsaved work on start.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from core.history import History
from core.projects.session import (
    HistorySnapshot,
    RecoveryEntry,
    RecoveryStore,
    load_history,
    norm_path,
    save_history,
)
from core.workers import Worker

if TYPE_CHECKING:
    from ui.main_window.main_window import MainWindow

log = logging.getLogger(__name__)

AUTOSAVE_CHOICES = (1, 2, 5, 10)  # minutes


class SessionKeeper(QObject):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.config = window.config
        self.store = RecoveryStore(self.config.data_dir)
        self.histories: dict[str, History] = {}  # normalised media path -> History
        self._session = RecoveryStore.new_session()
        self._written_key: str | None = None  # recovery copy written by this window
        self._writing = False
        self._workers: set[Worker] = set()  # keeps signals alive until the worker reports back
        self.last_autosave: float = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.autosave_now)
        self.apply_settings()

    # -------------------------------------------------------------- settings
    def apply_settings(self) -> None:
        minutes = self.config.autosave_minutes if self.config.autosave_minutes in AUTOSAVE_CHOICES else 2
        self.timer.setInterval(minutes * 60_000)
        if self.config.autosave_enabled:
            self.timer.start()
        else:
            self.timer.stop()

    # ------------------------------------------------------------ histories
    def history_for(self, media_path: str | Path | None, state) -> History:
        """The undo history of a clip; a state that differs from the stored
        one (the project was changed elsewhere) becomes one more step."""
        if media_path is None:
            return History(state)
        key = norm_path(media_path)
        history = self.histories.get(key)
        if history is None:
            history = self.histories[key] = History(state)
        elif history.current != state:
            history.record(state)
            history.break_merge()
        return history

    def forget(self, media_path: str | Path) -> None:
        self.histories.pop(norm_path(media_path), None)

    def reset(self, snapshots: dict[str, HistorySnapshot] | None = None) -> None:
        """Start over for another project, optionally with stored histories."""
        self.histories = {}
        for media, (undo, current, redo) in (snapshots or {}).items():
            history = History(current)
            history.restore(undo, current, redo)
            self.histories[norm_path(media)] = history

    def snapshots(self) -> dict[str, HistorySnapshot]:
        return {key: history.snapshot() for key, history in self.histories.items()}

    def load_for(self, project_path: Path) -> None:
        self.reset(load_history(self.config.data_dir, project_path))

    # -------------------------------------------------------------- autosave
    def _key(self) -> str:
        return RecoveryStore.key_for(self.window.project_path, self._session)

    def autosave_now(self) -> bool:
        """Write a recovery copy in the background; False when not needed."""
        w = self.window
        if not w.is_dirty or self._writing or not w.project.media:
            return False
        key = self._key()
        worker = Worker(self.store.write, key, w.project.to_dict(), self.snapshots(),
                        w.project_path)
        worker.signals.finished.connect(lambda _p, k=key, wk=worker: self._written(k, wk))
        worker.signals.failed.connect(lambda exc, wk=worker: self._write_failed(exc, wk))
        self._writing = True
        self._workers.add(worker)
        w.pool.start(worker)
        return True

    def _written(self, key: str, worker: Worker) -> None:
        self._workers.discard(worker)
        self._writing = False
        if self._written_key and self._written_key != key:
            self.store.discard(self._written_key)  # the project got a new name
        self._written_key = key
        if not self.window.is_dirty:  # saved while the copy was written
            self.discard_recovery()
            return
        self.last_autosave = time.time()
        self.window.statusBar().showMessage("Recovery copy saved", 2500)

    def _write_failed(self, exc, worker: Worker) -> None:
        self._workers.discard(worker)
        self._writing = False
        log.warning("Autosave failed: %s", exc)

    def discard_recovery(self) -> None:
        for key in {self._written_key, self._key()}:
            self.store.discard(key)
        self._written_key = None

    def on_saved(self) -> None:
        """The project file was written: keep its history, drop the copy."""
        self.discard_recovery()
        path = self.window.project_path
        if path is None:
            return
        worker = Worker(save_history, self.config.data_dir, path, self.snapshots())
        worker.signals.finished.connect(lambda _p, wk=worker: self._workers.discard(wk))
        worker.signals.failed.connect(lambda exc, wk=worker: self._history_failed(exc, wk))
        self._workers.add(worker)
        self.window.pool.start(worker)

    def _history_failed(self, exc, worker: Worker) -> None:
        self._workers.discard(worker)
        log.warning("Cannot save undo history: %s", exc)

    def finish(self) -> None:
        """The window closes normally: the user already chose Save or Discard."""
        self.timer.stop()
        self.discard_recovery()

    # -------------------------------------------------------------- recovery
    def adopt(self, entry: RecoveryEntry) -> None:
        """Continue writing to the restored copy until the project is saved."""
        if entry.key.startswith("u-"):
            self._session = entry.key[2:]
        self._written_key = entry.key

    def entries(self) -> list[RecoveryEntry]:
        mine = {self._written_key, self._key()}
        return [e for e in self.store.entries() if e.key not in mine]

    def offer_recovery(self, quiet: bool = True) -> bool:
        """Ask whether to restore unsaved work left by a crash.
        ``quiet`` skips the dialog when there is nothing to restore."""
        entries = self.entries()
        if not entries:
            if not quiet:
                self.window.statusBar().showMessage("No unsaved work to restore", 4000)
            return False
        dialog = RecoveryDialog(entries, self.store, self.window)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.chosen is None:
            return False
        return self.window.restore_recovery(dialog.chosen)


class RecoveryDialog(QDialog):
    """Lists recovery copies; Restore opens one, Delete removes it."""

    def __init__(self, entries: list[RecoveryEntry], store: RecoveryStore, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Restore unsaved work")
        self.setMinimumWidth(520)
        self.store = store
        self.chosen: RecoveryEntry | None = None
        layout = QVBoxLayout(self)
        intro = QLabel("The app was closed without saving these projects. "
                       "Restore one to continue where you left off.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.list = QListWidget()
        for entry in entries:
            when = time.strftime("%d.%m.%Y %H:%M", time.localtime(entry.saved_at))
            where = entry.original_path or "never saved"
            item = QListWidgetItem(f"{entry.project_name}  •  {when}\n{where}")
            item.setData(256, entry)
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda _item: self._restore())
        layout.addWidget(self.list)
        buttons = QDialogButtonBox()
        self.restore_button = QPushButton("Restore")
        self.restore_button.setDefault(True)
        self.delete_button = QPushButton("Delete")
        buttons.addButton(self.restore_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.delete_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton("Not now", QDialogButtonBox.ButtonRole.RejectRole)
        self.restore_button.clicked.connect(self._restore)
        self.delete_button.clicked.connect(self._delete)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _current(self) -> RecoveryEntry | None:
        item = self.list.currentItem()
        return item.data(256) if item is not None else None

    def _restore(self) -> None:
        self.chosen = self._current()
        if self.chosen is not None:
            self.accept()

    def _delete(self) -> None:
        entry = self._current()
        if entry is None:
            return
        self.store.discard(entry.key)
        self.list.takeItem(self.list.currentRow())
        if self.list.count() == 0:
            self.reject()

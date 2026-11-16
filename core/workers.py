"""Background execution helper so the GUI thread never blocks.

Usage::

    worker = Worker(probe_media, path, ffprobe)
    worker.signals.finished.connect(on_result)
    worker.signals.failed.connect(on_error)
    QThreadPool.globalInstance().start(worker)
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Signal

log = logging.getLogger(__name__)


class WorkerSignals(QObject):
    finished = Signal(object)  # result
    failed = Signal(object)  # exception instance


class Worker(QRunnable):
    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    def run(self) -> None:  # executed in a pool thread
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            log.debug("Worker failed: %s", traceback.format_exc())
            self.signals.failed.emit(exc)
        else:
            self.signals.finished.emit(result)

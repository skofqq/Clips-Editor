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


class TaskSignals(QObject):
    progress = Signal(float, str)  # fraction 0..1, message
    finished = Signal(object)
    failed = Signal(object)
    cancelled = Signal()


class CancellableTask(QRunnable):
    """Like :class:`Worker`, but ``fn`` receives ``progress`` and ``cancel``
    keyword arguments, and the task can be cancelled from the GUI thread.

    ``fn`` should raise any exception whose class name ends in
    ``Cancelled`` when it notices the cancel event.
    """

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        import threading

        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.cancel_event = threading.Event()
        self.signals = TaskSignals()
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        def progress(fraction: float, message: str = "") -> None:
            self.signals.progress.emit(float(fraction), message)

        try:
            result = self.fn(*self.args, progress=progress, cancel=self.cancel_event, **self.kwargs)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            if type(exc).__name__.endswith("Cancelled") or self.cancel_event.is_set():
                self.signals.cancelled.emit()
            else:
                log.debug("Task failed: %s", traceback.format_exc())
                self.signals.failed.emit(exc)
        else:
            if self.cancel_event.is_set():
                self.signals.cancelled.emit()
            else:
                self.signals.finished.emit(result)

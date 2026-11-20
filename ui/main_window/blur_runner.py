"""Blur → Follow object: finds the object under a blur box in every frame
of the blur (in the background) and stores the motion in the blur."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject

from core.vision.object_track import track_object
from core.workers import CancellableTask

if TYPE_CHECKING:
    from ui.main_window.main_window import MainWindow

log = logging.getLogger(__name__)


class BlurRunner(QObject):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.panel = window.blur_panel
        self.task: CancellableTask | None = None
        self.panel.follow_requested.connect(self.start)
        self.panel.follow_cancel_requested.connect(self.cancel)

    def start(self, blur_id: int) -> bool:
        w = self.window
        media, timeline = w.preview.media, w.timeline.timeline
        if self.task is not None:
            return False
        if media is None or media.video is None or timeline is None or w.ffmpeg is None:
            self.panel.set_busy(False, "Import a video first.")
            return False
        region = timeline.blur(blur_id)
        if region is None:
            return False
        at_us = min(max(w.timeline.playhead_us, region.start_us), region.end_us - 1)
        sw, sh = w.composition.source_size
        task = CancellableTask(track_object, w.ffmpeg.ffmpeg, Path(media.path), sw, sh, region.rect_at(at_us),
                               at_us, region.start_us, region.end_us, region.rect)
        task.signals.progress.connect(lambda _f, m: self.panel.set_busy(True, m))
        task.signals.finished.connect(lambda keys, p=media.path, b=blur_id: self._done(p, b, keys))
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(lambda: self._finish("Cancelled."))
        self.task = task
        self.panel.set_busy(True, "Starting…")
        w.pool.start(task)
        return True

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()

    def _finish(self, message: str) -> None:
        self.task = None
        self.panel.set_busy(False, message)

    def _done(self, media_path, blur_id: int, keys) -> None:
        w = self.window
        if w.preview.media is None or w.preview.media.path != media_path or w.timeline.timeline is None \
                or w.timeline.timeline.blur(blur_id) is None:
            self._finish("The clip or the box changed; nothing was applied.")
            return
        self.task = None
        w.timeline.update_blur(blur_id, track=tuple(keys))
        self.panel.set_busy(False, "The box follows the object." if keys else
                            "The object does not move, so the box stays in place.")
        w.statusBar().showMessage("Blur box follows the object", 5000)

    def _failed(self, error) -> None:
        log.warning("Object tracking failed: %s", error)
        self._finish(str(error))

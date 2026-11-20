"""Composition → Face → Auto-frame: runs face detection in the background
and puts the result (crop + follow track) into the webcam region."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject

from core.editor.composition import WEBCAM
from core.vision.autoframe import autoframe
from core.vision.faces import FaceDetector, download_model
from core.workers import CancellableTask

if TYPE_CHECKING:
    from ui.main_window.main_window import MainWindow

log = logging.getLogger(__name__)


class AutoframeRunner(QObject):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.panel = window.composition_panel
        self.task: CancellableTask | None = None
        self.detector_factory: Callable[[], FaceDetector] | None = None  # tests inject one
        self.panel.autoframe_requested.connect(self.start)
        self.panel.autoframe_cancel_requested.connect(self.cancel)

    def _default_factory(self) -> Callable[[], FaceDetector]:
        models = self.window.config.data_dir / "models"
        return lambda: FaceDetector(download_model(models))

    def start(self, follow: bool = True) -> bool:
        w = self.window
        media, timeline = w.preview.media, w.timeline.timeline
        if self.task is not None:
            return False
        if media is None or media.video is None or timeline is None or w.ffmpeg is None:
            self.panel.set_autoframe_busy(False, "Import a video first.")
            return False
        controller = w.composition
        comp = controller.composition
        sw, sh = controller.source_size
        slot = comp.slots().get(WEBCAM)
        crop_px = comp.crop_px(WEBCAM, sw, sh)
        aspect = (slot.w / slot.h) if slot is not None and slot.h else (crop_px.w / max(1.0, crop_px.h))
        task = CancellableTask(
            autoframe, w.ffmpeg.ffmpeg, Path(media.path), self.detector_factory or self._default_factory(),
            timeline.start_us, timeline.end_us, sw, sh, aspect,
            prefer=comp.webcam.at(controller.time_us).crop, follow=follow,
        )
        task.signals.progress.connect(lambda f, m: self.panel.set_autoframe_busy(True, f"{m} {f * 100:.0f}%"))
        task.signals.finished.connect(lambda result, p=media.path: self._done(p, result))
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(lambda: self._finish("Cancelled."))
        self.task = task
        self.panel.set_autoframe_busy(True, "Starting…")
        w.pool.start(task)
        return True

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()

    def _finish(self, message: str) -> None:
        self.task = None
        self.panel.set_autoframe_busy(False, message)

    def _done(self, media_path, result) -> None:
        w = self.window
        if w.preview.media is None or w.preview.media.path != media_path:
            self._finish("The clip changed; nothing was applied.")
            return
        self.task = None
        controller = w.composition
        controller.select(WEBCAM)
        controller.set_track(WEBCAM, result.crop, result.track)  # one undo step
        self.panel.set_autoframe_busy(False)
        share = result.found / max(1, result.total)
        note = (f"Face found in {share:.0%} of the clip. "
                + ("The crop follows it." if result.track else "It stays in place, so the crop is still."))
        self.panel.face_status.setText(note)
        w.statusBar().showMessage("Webcam framed around the face", 5000)

    def _failed(self, error) -> None:
        log.warning("Auto-frame failed: %s", error)
        self._finish(str(error))

"""Render queue: exports wait their turn and render one after another in the
background, so the next clip can be edited meanwhile. A job can also carry an
upload: once rendered, the video goes to YouTube before the next job starts.

A job is a snapshot of the edit (all edit values are immutable), the export
settings and the output file; changing the edit later does not change a
queued job."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThreadPool, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QWidget,
)

from core.render.job import render
from core.workers import CancellableTask
from ui.widgets.card import Card

log = logging.getLogger(__name__)

WAITING, RENDERING, DONE, FAILED, CANCELLED = "waiting", "rendering", "done", "failed", "cancelled"
UPLOADING, UPLOADED, UPLOAD_FAILED = "uploading", "uploaded", "upload_failed"


@dataclass(eq=False)
class UploadPlan:
    """What to publish once the job is rendered (a snapshot, like the edit)."""

    channel_id: str
    channel_title: str
    metadata: object  # VideoMetadata
    captions_srt: str = ""
    captions_language: str = ""
    thumbnail: object = None  # callable making the cover's JPEG bytes, or None
    client: object = None  # YouTubeClient for the channel
    result: object = None  # UploadResult once uploaded


@dataclass(eq=False)
class RenderJob:
    source: object  # MediaInfo
    composition: object
    timeline: object
    settings: object  # RenderSettings
    output: Path
    encoders: list | None = None
    subtitles: dict = field(default_factory=dict)  # captions / subtitle_style for render()
    tag: object = None  # the caller's marker (the edit state it was made from)
    upload: UploadPlan | None = None
    status: str = WAITING
    progress: float = 0.0
    message: str = ""

    @property
    def name(self) -> str:
        return Path(self.output).name


class RenderQueue(QObject):
    """Runs waiting jobs one at a time."""

    changed = Signal()
    job_finished = Signal(object)  # RenderJob (rendered)
    job_uploaded = Signal(object)  # RenderJob (uploaded; job.upload.result is set)

    def __init__(self, ffmpeg, parent: QObject | None = None, runner=render, uploader=None) -> None:
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self.runner = runner
        # uploader(job, progress=, cancel=) -> UploadResult, run off the GUI thread
        self.uploader = uploader
        self.jobs: list[RenderJob] = []
        self.running = False  # the user pressed Start and the queue has not run dry
        self._task: CancellableTask | None = None
        self._current: RenderJob | None = None

    # ------------------------------------------------------------ editing
    def add(self, job: RenderJob) -> None:
        self.jobs.append(job)
        self.changed.emit()
        if self.running and self._task is None:
            self._next()

    def remove(self, job: RenderJob) -> None:
        if job is self._current:
            return
        self.jobs = [j for j in self.jobs if j is not job]
        self.changed.emit()

    def clear_finished(self) -> None:
        self.jobs = [j for j in self.jobs if j.status in (WAITING, RENDERING, UPLOADING)]
        self.changed.emit()

    @property
    def waiting(self) -> int:
        return sum(1 for j in self.jobs if j.status == WAITING)

    @property
    def busy(self) -> bool:
        return self._task is not None

    # ------------------------------------------------------------ running
    def start(self) -> None:
        self.running = True
        if self._task is None:
            self._next()
        self.changed.emit()

    def stop(self) -> None:
        """Stop after cancelling the job being rendered; waiting jobs stay."""
        self.running = False
        if self._task is not None:
            self._task.cancel()
        self.changed.emit()

    def _next(self) -> None:
        job = next((j for j in self.jobs if j.status == WAITING), None)
        if job is None or self.ffmpeg is None:
            self.running = False
            self.changed.emit()
            return
        job.status, job.progress, job.message = RENDERING, 0.0, "Starting…"
        task = CancellableTask(self.runner, job.source, job.composition, job.timeline, job.settings, job.output,
                               self.ffmpeg.ffmpeg, self.ffmpeg.ffprobe, job.encoders, **job.subtitles)
        task.signals.progress.connect(lambda f, m, j=job: self._progress(j, f, m))
        task.signals.finished.connect(lambda result, j=job: self._rendered(j))
        task.signals.failed.connect(lambda exc, j=job: self._done(j, FAILED, str(exc)))
        task.signals.cancelled.connect(lambda j=job: self._done(j, CANCELLED, "Cancelled"))
        self._task, self._current = task, job
        self.changed.emit()
        QThreadPool.globalInstance().start(task)

    def _rendered(self, job: RenderJob) -> None:
        if job.upload is None or self.uploader is None:
            self._done(job, DONE, "")
            return
        self.job_finished.emit(job)  # the file is there even if the upload fails
        job.status, job.progress, job.message = UPLOADING, 0.0, "Starting upload…"
        task = CancellableTask(self.uploader, job)
        task.signals.progress.connect(lambda f, m, j=job: self._progress(j, f, m))
        task.signals.finished.connect(lambda result, j=job: self._uploaded(j, result))
        task.signals.failed.connect(lambda exc, j=job: self._done(j, UPLOAD_FAILED, str(exc)))
        task.signals.cancelled.connect(lambda j=job: self._done(j, UPLOAD_FAILED, "Upload cancelled"))
        self._task = task
        self.changed.emit()
        QThreadPool.globalInstance().start(task)

    def _uploaded(self, job: RenderJob, result) -> None:
        job.upload.result = result
        notes = [n for n in (result.captions if result.captions not in ("", "added") else "",
                             result.thumbnail if result.thumbnail not in ("", "set") else "") if n]
        self._done(job, UPLOADED, result.url + ("  ·  " + notes[0] if notes else ""), emit=False)
        self.job_uploaded.emit(job)

    def _progress(self, job: RenderJob, fraction: float, message: str) -> None:
        job.progress, job.message = fraction, message
        self.changed.emit()

    def _done(self, job: RenderJob, status: str, message: str, emit: bool = True) -> None:
        job.status, job.message = status, message
        job.progress = 1.0 if status in (DONE, UPLOADED) else job.progress
        if status in (FAILED, UPLOAD_FAILED):
            log.error("Queued job %s failed (%s): %s", job.output, status, message)
        self._task = self._current = None
        if status == DONE and emit:
            self.job_finished.emit(job)
        if self.running:
            self._next()
        else:
            self.changed.emit()


_STATUS = {WAITING: "Waiting", RENDERING: "Rendering", DONE: "✓ Done", FAILED: "Failed", CANCELLED: "Cancelled",
           UPLOADING: "Uploading", UPLOADED: "✓ On YouTube", UPLOAD_FAILED: "Rendered, upload failed"}
RENDERED = (DONE, UPLOADED, UPLOAD_FAILED)  # the file exists


class QueuePanel(Card):
    """The queue on the Render page: jobs, Start / Stop, Remove, Clear finished."""

    upload_requested = Signal(object)  # Path

    def __init__(self, queue: RenderQueue, parent: QWidget | None = None) -> None:
        super().__init__("Render queue", "Add exports with “Add to queue”, then Start: they render one after "
                         "another while you edit the next clip. Ticked “Upload to YouTube” jobs are "
                         "published right after rendering.", parent)
        self.queue = queue
        self.list = QListWidget(self)
        self.list.setMinimumHeight(90)
        self.start_button = QPushButton("Start", self)
        self.start_button.setObjectName("primary")
        self.stop_button = QPushButton("Stop", self)
        self.remove_button = QPushButton("Remove", self)
        self.clear_button = QPushButton("Clear finished", self)
        self.upload_button = QPushButton("Upload…", self)
        self.open_button = QPushButton("Open on YouTube", self)
        self.summary = QLabel(self)
        self.summary.setObjectName("muted")
        self.body.addWidget(self.list)
        row = QHBoxLayout()
        for b in (self.remove_button, self.clear_button, self.upload_button, self.open_button):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.summary)
        row.addWidget(self.stop_button)
        row.addWidget(self.start_button)
        self.body.addLayout(row)
        self.start_button.clicked.connect(queue.start)
        self.stop_button.clicked.connect(queue.stop)
        self.remove_button.clicked.connect(self._remove)
        self.clear_button.clicked.connect(queue.clear_finished)
        self.upload_button.clicked.connect(self._upload)
        self.open_button.clicked.connect(self._open)
        self.list.currentRowChanged.connect(lambda _r: self._update_buttons())
        queue.changed.connect(self.refresh)
        self.refresh()

    def _selected(self) -> RenderJob | None:
        row = self.list.currentRow()
        return self.queue.jobs[row] if 0 <= row < len(self.queue.jobs) else None

    def refresh(self) -> None:
        row = self.list.currentRow()
        self.list.clear()
        for job in self.queue.jobs:
            text = f"{_STATUS[job.status]}   {job.name}"
            if job.upload is not None and job.status in (WAITING, RENDERING):
                text += "   → YouTube"
            if job.status in (RENDERING, UPLOADING):
                text += f"   {job.progress * 100:.0f}%  {job.message}"
            elif job.status in (FAILED, UPLOAD_FAILED, UPLOADED):
                text += f"   {job.message.splitlines()[0] if job.message else ''}"
            item = QListWidgetItem(text)
            item.setToolTip(str(job.output))
            self.list.addItem(item)
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)
        waiting = self.queue.waiting
        self.summary.setText(f"{len(self.queue.jobs)} in the queue · {waiting} waiting" if self.queue.jobs else "")
        self.setVisible(bool(self.queue.jobs))
        self._update_buttons()

    def _update_buttons(self) -> None:
        job = self._selected()
        self.start_button.setVisible(not self.queue.running)
        self.start_button.setEnabled(self.queue.waiting > 0)
        self.stop_button.setVisible(self.queue.running)
        self.remove_button.setEnabled(job is not None and job.status not in (RENDERING, UPLOADING))
        self.clear_button.setEnabled(any(j.status not in (WAITING, RENDERING, UPLOADING) for j in self.queue.jobs))
        self.upload_button.setEnabled(job is not None and job.status in (DONE, UPLOAD_FAILED))
        self.open_button.setVisible(job is not None and job.status == UPLOADED)

    def _remove(self) -> None:
        job = self._selected()
        if job is not None:
            self.queue.remove(job)

    def _upload(self) -> None:
        job = self._selected()
        if job is not None and job.status in (DONE, UPLOAD_FAILED):
            self.upload_requested.emit(Path(job.output))

    def _open(self) -> None:
        job = self._selected()
        if job is not None and job.upload is not None and job.upload.result is not None:
            QDesktopServices.openUrl(QUrl(job.upload.result.url))


__all__ = ["CANCELLED", "DONE", "FAILED", "QueuePanel", "RENDERED", "RENDERING", "RenderJob", "RenderQueue",
           "UPLOADED", "UPLOADING", "UPLOAD_FAILED", "UploadPlan", "WAITING"]

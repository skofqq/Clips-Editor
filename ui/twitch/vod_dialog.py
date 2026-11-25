"""File → Download stream recording: paste a ``twitch.tv/videos/…`` link,
choose a part (e.g. 1:20:00 – 2:00:00) and a quality; only that part is
downloaded, added to Media, and its chat is kept for the Highlights tab.

The window is not modal: the editor stays usable while a long part
downloads. ``downloaded`` is emitted with ``(VodDownload, TwitchInfo or
None, notes)`` when everything is done."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThreadPool, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.twitch import vod
from core.twitch.download import TwitchError
from core.workers import CancellableTask, Worker
from ui.widgets.m3_progress import WavyProgressBar

log = logging.getLogger(__name__)


def default_folder() -> Path:
    return Path.home() / "Videos" / "Twitch VODs"


def run_download(info: vod.VodInfo, start_s: float, end_s: float | None, folder: Path, ffmpeg: str,
                 quality: str, chat: bool, cache_dir: Path, fill: bool, *, progress, cancel,
                 get=None, post=None, link_info=None):
    """The whole job (pool thread): video, then chat, then the Data page facts.
    A chat or facts failure does not lose the video; it becomes a note."""
    video_share = 0.85 if chat else 1.0

    def video_progress(fraction: float, text: str) -> None:
        progress(fraction * video_share, text)

    result = vod.download_vod(info, start_s, end_s, folder, ffmpeg, quality=quality,
                              progress=video_progress, cancel=cancel, get=get)
    notes = list(result.notes)
    if chat:
        from core.highlights.chat import ChatError  # noqa: PLC0415

        def chat_progress(fraction: float, text: str) -> None:
            progress(video_share + fraction * (1 - video_share) * 0.95, text)

        try:
            count = vod.download_chat(result, cache_dir, progress=chat_progress, cancel=cancel, post=post)
            notes.append(f"Chat: {count:,} messages, ready in Highlights.".replace(",", " "))
        except ChatError as exc:
            log.warning("VOD chat: %s", exc)
            notes.append(f"The chat was not downloaded: {exc}")
    facts = None
    if fill:
        progress(0.99, "Asking Twitch about the streamer and game…")
        from core.metadata import twitch_link as tl  # noqa: PLC0415

        try:
            facts = (link_info or tl.fetch_info)(tl.video_url(info.video_id, int(round(result.start_s))))
        except Exception as exc:  # noqa: BLE001 - filling is a bonus
            log.warning("VOD facts: %s", exc)
    progress(1.0, "Done")
    return result, facts, notes


class VodDownloadDialog(QDialog):
    downloaded = Signal(object, object, object)  # VodDownload, TwitchInfo | None, list[str]

    def __init__(self, *, folder: str = "", quality: str = "", chat: bool = True, fill: bool = True,
                 url: str = "", ffmpeg: str = "", cache_dir: Path | None = None, parent: QWidget | None = None,
                 ydl_factory=None, get=None, post=None, link_info=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Download stream recording")
        self.setMinimumWidth(600)
        self.setModal(False)
        self.ffmpeg = ffmpeg
        self.cache_dir = cache_dir or Path.home()
        self.info: vod.VodInfo | None = None
        self.result = None
        self._wanted_quality = quality
        self._ydl_factory, self._get, self._post, self._link_info = ydl_factory, get, post, link_info
        self._info_worker: Worker | None = None
        self._info_for = ""
        self._task: CancellableTask | None = None

        layout = QVBoxLayout(self)
        intro = QLabel("Paste a link to a Twitch stream recording and choose the part you need. "
                       "Only that part is downloaded, then it opens in Media and its chat goes to Highlights.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://www.twitch.tv/videos/1234567890")
        self.url_edit.setClearButtonEnabled(True)
        layout.addWidget(self.url_edit)
        self.info_label = QLabel()
        self.info_label.setObjectName("secondaryText")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        form = QFormLayout()
        span = QHBoxLayout()
        self.start_edit = QLineEdit("0:00:00")
        self.start_edit.setPlaceholderText("1:20:00")
        self.end_edit = QLineEdit()
        self.end_edit.setPlaceholderText("to the end")
        for edit in (self.start_edit, self.end_edit):
            edit.setMaximumWidth(110)
            edit.textChanged.connect(self._update_summary)
        span.addWidget(QLabel("from"))
        span.addWidget(self.start_edit)
        span.addWidget(QLabel("to"))
        span.addWidget(self.end_edit)
        self.whole_button = QPushButton("Whole stream")
        self.whole_button.clicked.connect(self._whole)
        span.addWidget(self.whole_button)
        span.addStretch(1)
        form.addRow("Part", span)
        self.quality_combo = QComboBox()
        self.quality_combo.currentIndexChanged.connect(self._update_summary)
        form.addRow("Quality", self.quality_combo)
        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit(folder or str(default_folder()))
        folder_row.addWidget(self.folder_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(browse)
        form.addRow("Save to", folder_row)
        layout.addLayout(form)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.chat_check = QCheckBox("Download the chat of this part for Highlights")
        self.chat_check.setChecked(chat)
        self.fill_check = QCheckBox("Fill in streamer, game and date on the Data page")
        self.fill_check.setChecked(fill)
        layout.addWidget(self.chat_check)
        layout.addWidget(self.fill_check)

        self.progress = WavyProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.buttons = QDialogButtonBox()
        self.download_button = self.buttons.addButton("Download", QDialogButtonBox.ButtonRole.AcceptRole)
        self.download_button.setDefault(True)
        self.cancel_button = self.buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.download_button.clicked.connect(self.start)
        self.cancel_button.clicked.connect(self._cancel)
        layout.addWidget(self.buttons)

        self._info_timer = QTimer(self)
        self._info_timer.setSingleShot(True)
        self._info_timer.setInterval(500)
        self._info_timer.timeout.connect(self._fetch_info)
        self.url_edit.textChanged.connect(self._url_changed)

        if not url:
            clip_text = QGuiApplication.clipboard().text() if QGuiApplication.instance() else ""
            url = clip_text if vod.parse_vod_link(clip_text) else ""
        self.url_edit.setText(url)
        self._url_changed()

    # ------------------------------------------------------------- options
    @property
    def folder(self) -> str:
        return self.folder_edit.text().strip()

    @property
    def quality(self) -> str:
        return str(self.quality_combo.currentData() or "")

    def span(self) -> tuple[int | None, int | None, str]:
        """(start, end or None, problem text)."""
        start = vod.parse_time(self.start_edit.text()) if self.start_edit.text().strip() else 0
        end_text = self.end_edit.text().strip()
        end = vod.parse_time(end_text) if end_text else None
        if start is None:
            return None, None, "The start time is not readable. Write it like 1:20:00."
        if end_text and end is None:
            return None, None, "The end time is not readable. Write it like 2:00:00."
        length = self.info.duration_s if self.info is not None else 0
        if end is not None and end <= start:
            return None, None, "The end must be after the start."
        if length and start >= length:
            return None, None, f"The recording is only {vod.format_time(length)} long."
        if length and end is not None and end > length:
            end = None  # past the end = to the end
        return start, end, ""

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Save stream recordings to", self.folder)
        if folder:
            self.folder_edit.setText(folder)

    def _whole(self) -> None:
        self.start_edit.setText("0:00:00")
        self.end_edit.clear()

    # ---------------------------------------------------------- link facts
    def _url_changed(self) -> None:
        ref = vod.parse_vod_link(self.url_edit.text())
        if ref is None:
            self.info = None
            self.quality_combo.clear()
            self.info_label.setText("This does not look like a link to a stream recording."
                                    if self.url_edit.text().strip() else "")
            self._update_summary()
            return
        if ref.offset_s and self._task is None:
            self.start_edit.setText(vod.format_time(ref.offset_s))
        if self.info is not None and self.info.video_id == ref.video_id:
            return
        self.info = None
        self.info_label.setText("Reading the recording…")
        self._update_summary()
        self._info_timer.start()

    def _fetch_info(self) -> None:
        ref = vod.parse_vod_link(self.url_edit.text())
        if ref is None or self._info_worker is not None:
            if ref is not None:
                self._info_timer.start()  # one is still running: ask again after it
            return
        worker = Worker(vod.fetch_vod_info, ref, self._ydl_factory)
        worker.signals.finished.connect(lambda info, w=worker: self._info_done(w, info, None))
        worker.signals.failed.connect(lambda exc, w=worker: self._info_done(w, None, exc))
        self._info_worker, self._info_for = worker, ref.video_id
        QThreadPool.globalInstance().start(worker)

    def _info_done(self, worker, info, error) -> None:
        if worker is not self._info_worker:
            return
        self._info_worker = None
        ref = vod.parse_vod_link(self.url_edit.text())
        if ref is None or ref.video_id != self._info_for:
            if ref is not None:
                self._info_timer.start()
            return
        if info is None:
            self.info = None
            self.info_label.setText(f"Could not read the recording: {error}")
            self._update_summary()
            return
        self.info = info
        parts = [p for p in (info.channel, f"“{info.title}”" if info.title else "", info.created) if p]
        if info.duration_s:
            parts.append(vod.format_time(info.duration_s))
        self.info_label.setText(" • ".join(parts))
        self.quality_combo.blockSignals(True)
        self.quality_combo.clear()
        for q in info.qualities:
            self.quality_combo.addItem(q.label, q.name)
        chosen = info.quality(self._wanted_quality)
        if chosen is not None:
            self.quality_combo.setCurrentIndex(max(0, self.quality_combo.findData(chosen.name)))
        self.quality_combo.blockSignals(False)
        self._update_summary()

    def _update_summary(self) -> None:
        if self.quality_combo.currentData():
            self._wanted_quality = self.quality
        start, end, problem = self.span()
        ready = self.info is not None and not problem and self._task is None
        self.download_button.setEnabled(ready)
        if self.info is None:
            self.summary.setText("")
            return
        if problem:
            self.summary.setText(problem)
            return
        stop = end if end is not None else self.info.duration_s
        length = max(0.0, stop - start)
        text = f"{vod.format_time(length)} of video ({vod.format_time(start)} – {vod.format_time(stop)})"
        chosen = self.info.quality(self.quality)
        size = chosen.size_mb(length) if chosen is not None else 0
        if size:
            text += f", about {size / 1024:.1f} GB" if size >= 1024 else f", about {size:.0f} MB"
        text += ". The edges are rounded out to Twitch's 10-second pieces."
        self.summary.setText(text)

    # ---------------------------------------------------------------- run
    def start(self) -> bool:
        start, end, problem = self.span()
        if self.info is None or problem or self._task is not None:
            return False
        if not self.ffmpeg:
            self.status.setText("FFmpeg is needed to join the pieces. See Help → FFmpeg.")
            return False
        task = CancellableTask(run_download, self.info, start, end, Path(self.folder), self.ffmpeg, self.quality,
                               self.chat_check.isChecked(), self.cache_dir, self.fill_check.isChecked(),
                               get=self._get, post=self._post, link_info=self._link_info)
        task.signals.progress.connect(self._progress)
        task.signals.finished.connect(self._done)
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(self._cancelled)
        self._task = task
        self._set_busy(True)
        QThreadPool.globalInstance().start(task)
        return True

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        self.progress.setValue(0)
        for widget in (self.url_edit, self.start_edit, self.end_edit, self.whole_button, self.quality_combo,
                       self.folder_edit, self.chat_check, self.fill_check):
            widget.setEnabled(not busy)
        self.cancel_button.setText("Cancel" if busy else "Close")
        self._update_summary()

    def _progress(self, fraction: float, message: str) -> None:
        self.progress.setValue(int(fraction * 1000))
        if message:
            self.status.setText(message)

    def _done(self, outcome) -> None:
        self._task = None
        result, facts, notes = outcome
        self.result = result
        self._set_busy(False)
        self.status.setText(f"Saved {result.path.name}. " + " ".join(notes))
        self.downloaded.emit(result, facts, notes)
        self.accept()

    def _failed(self, error) -> None:
        self._task = None
        self._set_busy(False)
        message = str(error) if isinstance(error, TwitchError) else f"{type(error).__name__}: {error}"
        self.status.setText(f"Download failed: {message}\n\nPress Download again to continue where it stopped.")

    def _cancelled(self) -> None:
        self._task = None
        self._set_busy(False)
        self.status.setText("Cancelled. The pieces downloaded so far are kept: Download continues from them.")

    def _cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self.status.setText("Cancelling…")
        else:
            self.reject()

    @property
    def busy(self) -> bool:
        return self._task is not None

    def reject(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self.status.setText("Cancelling…")
            return  # closes after the task reports back
        super().reject()

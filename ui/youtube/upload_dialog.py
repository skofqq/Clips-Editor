"""Upload a rendered video to YouTube: file info, channel, metadata, progress."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QDateTime, QThreadPool, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.media.errors import MediaError
from core.media.probe import MediaInfo, probe_media
from core.timeline.timebase import format_timecode
from core.workers import CancellableTask
from core.youtube.errors import YouTubeAuthError, YouTubeError
from core.youtube.history import UploadHistory, UploadRecord
from core.youtube.metadata import (
    CATEGORIES,
    DEFAULT_CATEGORY,
    MAX_DESCRIPTION_BYTES,
    MAX_TITLE,
    PRIVACY,
    PRIVATE,
    VideoMetadata,
    parse_tags,
    tags_length,
)
from core.youtube.service import YouTubeService

log = logging.getLogger(__name__)

PRIVACY_TEXT = {"private": "Private", "unlisted": "Unlisted", "public": "Public"}
LANGUAGES = (("", "Not set"), ("ru", "Русский"), ("en", "English"))
SHORTS_MAX_S = 180


def default_title(path: Path) -> str:
    stem = path.stem
    for suffix in ("_short",):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem.replace("_", " ").strip()


def _upload(client, path, metadata, progress=None, cancel=None):
    return client.upload(path, metadata, progress, cancel)


class UploadDialog(QDialog):
    def __init__(
        self,
        service: YouTubeService,
        history: UploadHistory,
        file: Path,
        ffprobe: Path | None,
        defaults: dict | None = None,
        manage_accounts=None,  # callable opening the accounts dialog
        parent: QWidget | None = None,
        metadata=None,  # ClipMetadata from the project's Metadata tab
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Upload to YouTube")
        self.setMinimumWidth(600)
        self.service = service
        self.history = history
        self.file = Path(file)
        self.manage_accounts = manage_accounts
        self.result = None
        self._task: CancellableTask | None = None
        self._close_when_stopped = False
        self.info: MediaInfo | None = None
        if ffprobe is not None:
            try:
                self.info = probe_media(self.file, ffprobe)
            except MediaError as exc:
                log.warning("Could not probe %s: %s", self.file, exc)
        defaults = defaults or {}

        # ------------------------------------------------------ widgets
        self.file_label = QLabel(self)
        self.file_label.setWordWrap(True)
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.channel_combo = QComboBox(self)
        self.manage_button = QPushButton("Accounts…", self)
        self.title_edit = QLineEdit(default_title(self.file), self)
        self.title_count = QLabel(self)
        self.description_edit = QPlainTextEdit(self)
        self.description_edit.setPlaceholderText("Description, links, #hashtags")
        self.description_edit.setMinimumHeight(110)
        self.description_count = QLabel(self)
        self._default_tags = str(defaults.get("tags", ""))
        self.tags_edit = QLineEdit(self._default_tags, self)
        self.tags_edit.setPlaceholderText("twitch, shorts, streamer name")
        self.category_combo = QComboBox(self)
        for cid, name in CATEGORIES.items():
            self.category_combo.addItem(name, cid)
        self.privacy_combo = QComboBox(self)
        for value in PRIVACY:
            self.privacy_combo.addItem(PRIVACY_TEXT[value], value)
        self.schedule_check = QCheckBox("Publish at", self)
        self.schedule_edit = QDateTimeEdit(self)
        self.schedule_edit.setCalendarPopup(True)
        self.schedule_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.schedule_edit.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        self.language_combo = QComboBox(self)
        for code, name in LANGUAGES:
            self.language_combo.addItem(name, code)
        self.kids_check = QCheckBox("Made for kids", self)
        self.problems_label = QLabel(self)
        self.problems_label.setWordWrap(True)
        self.problems_label.setStyleSheet("color: #ff6b81")
        self.note_label = QLabel(self)
        self.note_label.setObjectName("muted")
        self.note_label.setWordWrap(True)
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.status_label.setOpenExternalLinks(True)
        self.upload_button = QPushButton("Upload", self)
        self.upload_button.setObjectName("primary")
        self.cancel_button = QPushButton("Cancel upload", self)
        self.copy_button = QPushButton("Copy link", self)
        self.close_button = QPushButton("Close", self)

        # ------------------------------------------------------ layout
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("File", self.file_label)
        channel = QHBoxLayout()
        channel.addWidget(self.channel_combo, 1)
        channel.addWidget(self.manage_button)
        form.addRow("Channel", channel)
        title = QHBoxLayout()
        title.addWidget(self.title_edit, 1)
        title.addWidget(self.title_count)
        form.addRow("Title", title)
        description = QVBoxLayout()
        description.addWidget(self.description_edit, 1)
        description.addWidget(self.description_count, 0, Qt.AlignmentFlag.AlignRight)
        form.addRow("Description", description)
        form.addRow("Tags", self.tags_edit)
        form.addRow("Category", self.category_combo)
        visibility = QHBoxLayout()
        visibility.addWidget(self.privacy_combo, 1)
        visibility.addWidget(self.schedule_check)
        visibility.addWidget(self.schedule_edit, 1)
        form.addRow("Visibility", visibility)
        extra = QHBoxLayout()
        extra.addWidget(self.language_combo, 1)
        extra.addWidget(self.kids_check)
        form.addRow("Language", extra)
        buttons = QHBoxLayout()
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.upload_button)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.note_label)
        layout.addWidget(self.problems_label)
        layout.addWidget(self.progress)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        # ------------------------------------------------------ defaults
        self._select(self.category_combo, defaults.get("category", DEFAULT_CATEGORY))
        self._select(self.privacy_combo, defaults.get("privacy", PRIVATE))
        self._select(self.language_combo, defaults.get("language", ""))
        self.from_metadata = False
        if metadata is not None and metadata.title.strip():
            self.from_metadata = True
            self.title_edit.setText(metadata.title)
            self.description_edit.setPlainText(metadata.description)
            self.tags_edit.setText(", ".join(metadata.tags))
            if metadata.generated is not None and metadata.generated.category_id:
                self._select(self.category_combo, metadata.generated.category_id)

        for signal in (self.title_edit.textChanged, self.description_edit.textChanged, self.tags_edit.textChanged,
                       self.privacy_combo.currentIndexChanged, self.category_combo.currentIndexChanged,
                       self.schedule_check.toggled, self.schedule_edit.dateTimeChanged,
                       self.channel_combo.currentIndexChanged):
            signal.connect(self._validate)
        self.schedule_check.toggled.connect(self._update_schedule)
        self.manage_button.clicked.connect(self._manage)
        self.upload_button.clicked.connect(self.start_upload)
        self.cancel_button.clicked.connect(self.cancel_upload)
        self.copy_button.clicked.connect(self._copy_link)
        self.close_button.clicked.connect(self.reject)

        self._show_file()
        self.refresh_channels()
        self._update_schedule()
        self._set_busy(False)
        self.copy_button.setVisible(False)
        previous = history.for_file(self.file)
        if previous:
            last = previous[-1]
            self.status_label.setText(
                f"This file was already uploaded on {last.uploaded_at[:16].replace('T', ' ')}: "
                f"<a href='{last.url}'>{last.url}</a>"
            )

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _select(combo: QComboBox, data) -> None:
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _show_file(self) -> None:
        size = self.file.stat().st_size / 1_048_576 if self.file.exists() else 0
        lines = [str(self.file), f"{size:.1f} MB"]
        notes = []
        info = self.info
        if info is not None and info.video is not None:
            v = info.video
            audio = f" + {info.audio[0].codec}" if info.audio else ""
            lines[1] += (
                f" · {v.width}×{v.height} · {format_timecode(round(info.duration_s * 1e6))} · "
                f"{self.file.suffix.lstrip('.').upper()} · {v.codec}{audio}"
            )
            if v.width > v.height:
                notes.append("The video is horizontal, so YouTube will not show it as a Short.")
            if info.duration_s > SHORTS_MAX_S:
                notes.append("Longer than 3 minutes: YouTube will not treat it as a Short.")
        self.file_label.setText("\n".join(lines))
        self._file_notes = notes

    def refresh_channels(self) -> None:
        keep = self.channel_combo.currentData() or (self.service.active.id if self.service.active else None)
        self.channel_combo.blockSignals(True)
        self.channel_combo.clear()
        for channel in self.service.accounts:
            self.channel_combo.addItem(f"{channel.title} — {self.service.status(channel.id)}", channel.id)
        if not self.service.accounts:
            self.channel_combo.addItem("No channel connected: press Accounts…", None)
        self._select(self.channel_combo, keep)
        self.channel_combo.blockSignals(False)
        self._validate()

    def _update_schedule(self) -> None:
        scheduled = self.schedule_check.isChecked()
        self.schedule_edit.setEnabled(scheduled and self._task is None)
        if scheduled:
            self._select(self.privacy_combo, PRIVATE)
        self.privacy_combo.setEnabled(not scheduled and self._task is None)
        self._validate()

    def metadata(self) -> VideoMetadata:
        publish_at = None
        if self.schedule_check.isChecked():
            local = self.schedule_edit.dateTime().toPython()
            publish_at = local.astimezone().astimezone(timezone.utc)
        return VideoMetadata(
            title=self.title_edit.text(),
            description=self.description_edit.toPlainText(),
            tags=parse_tags(self.tags_edit.text()),
            category_id=self.category_combo.currentData(),
            privacy=self.privacy_combo.currentData(),
            publish_at=publish_at,
            language=self.language_combo.currentData(),
            made_for_kids=self.kids_check.isChecked(),
        )

    def problems(self) -> list[str]:
        issues = self.metadata().problems()
        if self.channel_combo.currentData() is None:
            issues.insert(0, "Connect a YouTube channel first (Accounts…).")
        if not self.file.is_file():
            issues.insert(0, "The video file no longer exists.")
        return issues

    def _validate(self) -> None:
        meta = self.metadata()
        self.title_count.setText(f"{len(meta.title.strip())}/{MAX_TITLE}")
        self.description_count.setText(f"{len(meta.description.encode('utf-8'))}/{MAX_DESCRIPTION_BYTES} bytes · "
                                       f"tags {tags_length(meta.tags)}/500")
        issues = self.problems()
        self.problems_label.setText("\n".join(issues))
        self.problems_label.setVisible(bool(issues))
        notes = list(getattr(self, "_file_notes", []))
        if getattr(self, "from_metadata", False):
            notes.append("Title, description and tags come from the project's Metadata tab; changes here "
                         "apply to this upload only.")
        if meta.publish_at is not None:
            notes.append("Scheduled videos stay private until the chosen time, then become public.")
        self.note_label.setText("\n".join(notes))
        self.note_label.setVisible(bool(notes))
        if self._task is None:
            self.upload_button.setEnabled(not issues)

    def _manage(self) -> None:
        if self.manage_accounts is not None:
            self.manage_accounts()
            self.refresh_channels()

    def defaults(self) -> dict:
        return {
            "category": self.category_combo.currentData(),
            "privacy": self.privacy_combo.currentData() if not self.schedule_check.isChecked() else PRIVATE,
            "language": self.language_combo.currentData(),
            "tags": self.tags_edit.text() if not self.from_metadata else self._default_tags,
        }

    # ------------------------------------------------------------ upload
    def _set_busy(self, busy: bool) -> None:
        for widget in (self.channel_combo, self.manage_button, self.title_edit, self.description_edit,
                       self.tags_edit, self.category_combo, self.language_combo, self.kids_check,
                       self.schedule_check):
            widget.setEnabled(not busy)
        self.upload_button.setVisible(not busy)
        self.cancel_button.setVisible(busy)
        self.close_button.setEnabled(not busy)
        self.progress.setVisible(busy)
        self._update_schedule()

    def start_upload(self) -> None:
        if self._task is not None or self.problems():
            return
        channel_id = self.channel_combo.currentData()
        metadata = self.metadata()
        client = self.service.client_for(channel_id)
        task = CancellableTask(_upload, client, self.file, metadata)
        task.signals.progress.connect(self._on_progress)
        task.signals.finished.connect(self._on_finished)
        task.signals.failed.connect(self._on_failed)
        task.signals.cancelled.connect(self._on_cancelled)
        self._task = task
        self._metadata = metadata
        self._channel_id = channel_id
        self.progress.setValue(0)
        self.status_label.setText("Starting upload…")
        self._set_busy(True)
        log.info("Uploading %s to channel %s", self.file, channel_id)
        QThreadPool.globalInstance().start(task)

    def cancel_upload(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self.status_label.setText("Cancelling…")

    def _on_progress(self, fraction: float, message: str) -> None:
        self.progress.setValue(int(fraction * 1000))
        self.status_label.setText(message)

    def _finish(self) -> None:
        self._task = None
        self._set_busy(False)
        self._validate()
        if self._close_when_stopped:
            super().reject()

    def _on_finished(self, result) -> None:
        self.result = result
        channel = next((c for c in self.service.accounts if c.id == self._channel_id), None)
        meta = self._metadata
        self.history.add(UploadRecord(
            uploaded_at=datetime.now().isoformat(timespec="seconds"),
            file=str(self.file),
            video_id=result.video_id,
            url=result.url,
            title=meta.title.strip(),
            channel_id=self._channel_id,
            channel_title=channel.title if channel else "",
            privacy=result.privacy,
            publish_at=result.publish_at,
        ))
        text = f"Uploaded. Video ID {result.video_id}: <a href='{result.url}'>{result.url}</a>"
        wanted = meta.body()["status"]["privacyStatus"]
        if result.privacy and result.privacy != wanted:
            text += (
                f"<br>YouTube set it to <b>{result.privacy}</b> instead of {wanted}: uploads from an unverified "
                "API project stay private until the project passes YouTube's audit. Change it in YouTube Studio."
            )
        elif result.publish_at:
            text += f"<br>Scheduled for {self.schedule_edit.dateTime().toString('dd.MM.yyyy HH:mm')}."
        self.status_label.setText(text)
        self.copy_button.setVisible(True)
        self._finish()
        self.upload_button.setText("Upload again")

    def _on_failed(self, error) -> None:
        message = str(error) if isinstance(error, YouTubeError) else f"Unexpected error: {error}"
        log.error("Upload failed: %s", message)
        self.status_label.setText("Upload failed.")
        self._finish()
        if not self._close_when_stopped:
            if isinstance(error, YouTubeAuthError):
                message += "\n\nOpen Accounts… and reconnect the channel."
            QMessageBox.warning(self, "Upload failed", message)

    def _on_cancelled(self) -> None:
        self.status_label.setText("Upload cancelled. Nothing was published.")
        self._finish()

    def _copy_link(self) -> None:
        if self.result is not None:
            QGuiApplication.clipboard().setText(self.result.url)
            self.status_label.setText(self.status_label.text() + "<br>Link copied.")

    def open_video(self) -> None:
        if self.result is not None:
            QDesktopServices.openUrl(QUrl(self.result.url))

    # ------------------------------------------------------------ closing
    def reject(self) -> None:
        if self._task is not None:
            answer = QMessageBox.question(self, "Upload to YouTube", "Stop the upload?")
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._close_when_stopped = True
            self.cancel_upload()
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._task is not None:
            event.ignore()
            self.reject()
            return
        super().closeEvent(event)


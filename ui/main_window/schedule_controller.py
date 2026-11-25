"""The Publish page's Schedule tab, wired to the rest of the window:
"Add to schedule" on the YouTube form, History records for what went out,
and the channel's best time for the slot rule."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from core.projects import clip_memory as cm
from core.publishing import schedule as sc
from core.workers import Worker
from core.youtube.history import UploadRecord
from ui.social.schedule_panel import SchedulePanel, _when_text

log = logging.getLogger(__name__)


class ScheduleController:
    def __init__(self, window) -> None:
        self.window = window
        self.store = sc.PublishSchedule(window.config.data_dir / "publish_schedule.json")
        self.panel = SchedulePanel(self.store, window.youtube, window.social, window.config, window.service_tabs)
        self.panel.uploaded.connect(self._uploaded)
        self.panel.posted.connect(self._posted)
        self.tab = window.service_tabs.add(self.panel, "Schedule", "calendar")
        self._workers: set[Worker] = set()

    # ------------------------------------------------------------ adding
    def add_from_form(self, entry: dict) -> sc.QueueItem:
        """``entry``: UploadDialog.queue_entry() — the form's text, captions, thumbnail job, channel."""
        w = self.window
        meta = entry["metadata"]
        channel = next((c for c in w.youtube.accounts if c.id == entry.get("channel_id")), None)
        item = sc.new_item(
            entry["file"], meta.title.strip(), description=meta.description, tags=tuple(meta.tags),
            category_id=str(meta.category_id), language=meta.language or "", made_for_kids=meta.made_for_kids,
            captions_srt=entry.get("captions_srt", ""), captions_language=entry.get("captions_language", ""),
            playlist=entry.get("playlist", ""), source=w._current_source(), variant=w._variant_text(),
            channel_id=channel.id if channel else "", channel_title=channel.title if channel else "")
        item = self.panel.add(item)
        job = entry.get("thumbnail")
        if callable(job):
            worker = Worker(job)
            worker.signals.finished.connect(lambda data, i=item.id, wk=worker: self._cover_made(wk, i, data))
            worker.signals.failed.connect(lambda exc, wk=worker: (self._workers.discard(wk),
                                                                  log.info("Schedule cover: %s", exc)))
            self._workers.add(worker)
            w.pool.start(worker)
        form = w.upload_widget
        when = _when_text(item) if item.when else "when a slot frees up (add days or times)"
        if form is not None:
            form.status_label.setText(f"Added to the schedule: goes public {when}. See the Schedule tab.")
        w.statusBar().showMessage(f"Scheduled “{item.title}” for {when}", 8000)
        w.service_tabs.set_current(self.tab)
        return item

    def _cover_made(self, worker, item_id: str, data) -> None:
        self._workers.discard(worker)
        if isinstance(data, (bytes, bytearray)) and data:
            self.store.set_cover(item_id, bytes(data))

    # ------------------------------------------------------------ results
    def _cover_job(self, item: sc.QueueItem):
        path = self.store.cover_path(item)
        return (lambda: path.read_bytes()) if path is not None else None

    @staticmethod
    def _local(item: sc.QueueItem) -> str:
        m = item.moment
        if m is None:
            return datetime.now().isoformat(timespec="seconds")
        return m.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")

    def _uploaded(self, item: sc.QueueItem, result) -> None:
        w = self.window
        w.upload_history.add(UploadRecord(
            uploaded_at=datetime.now().isoformat(timespec="seconds"), file=item.file, video_id=result.video_id,
            url=result.url, title=item.title, channel_id=item.channel_id, channel_title=item.channel_title,
            privacy=result.privacy, publish_at=result.publish_at))
        w._record_publication(cm.Publication(
            cm.YOUTUBE, result.url, item.title, self._local(item), item.source, result.video_id,
            item.channel_title, "scheduled", item.file, variant=item.variant), self._cover_job(item))
        w._analytics_loaded = ""

    def _posted(self, item: sc.QueueItem, service: str, url: str) -> None:
        w = self.window
        kind = cm.TIKTOK if service == sc.TIKTOK else cm.INSTAGRAM
        account = w.social.tiktok_name if kind == cm.TIKTOK else w.social.instagram_name
        w._record_publication(cm.Publication(
            kind, url, item.title, datetime.now().isoformat(timespec="seconds"), item.source, "", account or "", "",
            item.file, variant=item.variant), self._cover_job(item))

    # ------------------------------------------------------------ window
    def set_best_time(self, best) -> None:
        self.panel.set_best_time(best)

    @property
    def busy(self) -> bool:
        return self.panel.busy

    def stop(self) -> None:
        self.panel.timer.stop()
        self.panel.cancel()
        for task in (self.panel._post_task,):
            if task is not None:
                task.cancel()

    def has_file(self, path: str | Path) -> bool:
        key = str(Path(path)).casefold()
        return any(str(Path(i.file)).casefold() == key for i in self.store.items)

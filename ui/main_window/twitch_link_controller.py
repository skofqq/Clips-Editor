"""Two more ways the Data page fills itself:

* a Twitch clip or stream recording link: Twitch is asked in the background
  for streamer, game, clip title, date and stream link
  (core.metadata.twitch_link); a new clip also gets the rest from the
  streamer's last clip; Undo puts the fields back;
* clip title ideas from the transcript (core.metadata.said_titles), made again
  whenever the transcript changes.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer

from core.metadata import carry_over as co
from core.metadata import twitch_link as tl
from core.metadata.said_titles import title_ideas, waveform_loudness
from core.workers import Worker

log = logging.getLogger(__name__)


class TwitchLinkController:
    def __init__(self, window) -> None:
        self.window = window
        self._task: Worker | None = None
        self.panel.twitch_link_requested.connect(self.fill_from_link)
        self._ideas_timer = QTimer(window)
        self._ideas_timer.setSingleShot(True)
        self._ideas_timer.setInterval(300)
        self._ideas_timer.timeout.connect(self.refresh_ideas)
        window.transcript.changed.connect(self._ideas_timer.start)

    @property
    def panel(self):
        return self.window.metadata_panel

    @property
    def library(self):
        return self.window.metadata_library

    # ------------------------------------------------------------ link
    def fill_from_link(self, text: str) -> None:
        if tl.parse_link(text) is None:
            self.panel.status_label.setText("This is not a Twitch clip, stream recording or channel link.")
            return
        if self._task is not None:
            return
        worker = Worker(tl.fetch_info, text)
        worker.signals.finished.connect(lambda info, w=worker: self._fetched(w, info))
        worker.signals.failed.connect(lambda exc, w=worker: self._failed(w, exc))
        self._task = worker
        self.window._pending.add(worker)
        self.panel.set_link_busy(True)
        self.panel.status_label.setText("Asking Twitch about the link…")
        self.window.pool.start(worker)

    def _done(self, worker) -> bool:
        self.window._pending.discard(worker)
        if worker is not self._task:
            return False
        self._task = None
        self.panel.set_link_busy(False)
        return True

    def _failed(self, worker, exc) -> None:
        if self._done(worker):
            log.warning("Twitch link: %s", exc)
            self.panel.status_label.setText(str(exc))

    def _fetched(self, worker, info) -> None:
        if not self._done(worker):
            return
        self.apply(info)

    def apply(self, info) -> None:
        panel = self.panel
        before = panel.metadata()
        try:
            result = tl.apply_info(before, info, self.library)
        except Exception as exc:  # noqa: BLE001 - a library clash must not lose the user's fields
            log.warning("Twitch link: %s", exc)
            panel.status_label.setText(f"Could not fill the fields: {exc}")
            return
        metadata = result.metadata
        from_last = ""
        streamer = metadata.selection.streamer_id
        if streamer and panel.carry_check.isChecked() and co.fresh(before):
            last = co.last_clip_of(self.window.clip_memory, streamer, exclude=self.window._current_source())
            if last is not None:
                metadata = co.carry_over(metadata, last.metadata, self.library)
                if not metadata.selection.clip_title.strip():
                    metadata = tl.regenerate(metadata, self.library)
                from_last = " The rest is from the streamer's last clip."
        panel.refresh_library()
        panel._last_streamer = metadata.selection.streamer_id  # no second fill from the last clip
        filled = ", ".join(result.filled) or "nothing new"
        what = {tl.CLIP: "clip", tl.VIDEO: "stream recording", tl.CHANNEL: "channel"}[info.link.kind]
        text = f"Filled from the Twitch {what}: {filled}." + from_last
        if result.added:
            text += " Added to the library: " + ", ".join(result.added) + "."
        panel.show_filled(before, metadata, " ".join([text, *result.notes]))
        if info.stream_title and not info.clip_title:
            panel.status_label.setText(f"Stream title on Twitch: “{info.stream_title}”.")

    # ------------------------------------------------------------ ideas
    def refresh_ideas(self) -> None:
        w = self.window
        transcript = w.transcript.transcript
        if transcript is None:
            self.panel.set_title_ideas([])
            return
        try:
            loudness = waveform_loudness(getattr(w.timeline_panel.view, "_waveform", None))
            ideas = title_ideas(transcript, w.censor_dictionary, loudness)
        except Exception as exc:  # noqa: BLE001 - ideas are a convenience; never block editing
            log.warning("Title ideas: %s", exc)
            ideas = []
        self.panel.set_title_ideas(ideas)

"""Glue between the Highlights panel, the timeline and the project."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject

from core.audio.waveform import load_waveform
from core.highlights.finder import find_highlights
from core.workers import Worker

if TYPE_CHECKING:
    from ui.main_window.main_window import MainWindow

log = logging.getLogger(__name__)


class HighlightsController(QObject):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.panel = window.highlights_panel
        self.highlights: list = []
        self._worker: Worker | None = None
        self.panel.find_requested.connect(self.find)
        self.panel.play_requested.connect(self.play)
        self.panel.select_requested.connect(self.select)
        self.panel.keep_requested.connect(self.keep)
        view = window.timeline_panel.view
        view.highlight_clicked.connect(self._marker_clicked)

    # ------------------------------------------------------------ state
    def show_for_current(self) -> None:
        """A clip was opened: show its stored highlights."""
        w = self.window
        media = w.preview.media
        stored = w.project.highlights_for(str(media.path)) if media is not None else None
        self._set(list(stored or []))
        self.panel.set_enabled_for(media is not None, w.transcript.transcript is not None)

    def _set(self, highlights: list) -> None:
        self.highlights = highlights
        self.panel.set_highlights(highlights)
        self.window.timeline_panel.view.set_highlights(highlights)

    # ------------------------------------------------------------ search
    def find(self, sensitivity: str = "normal") -> bool:
        w = self.window
        media, timeline = w.preview.media, w.timeline.timeline
        if media is None or timeline is None or self._worker is not None:
            return False
        transcript = w.transcript.transcript
        ffmpeg = w.ffmpeg.ffmpeg if w.ffmpeg is not None else None
        cache = w.config.cache_dir

        def job():
            waveform = None
            if media.has_audio and ffmpeg is not None:
                waveform = load_waveform(media.path, ffmpeg, cache)
            return find_highlights(waveform, transcript, timeline.duration_us, sensitivity)

        worker = Worker(job)
        worker.signals.finished.connect(lambda result, p=media.path: self._found(p, result))
        worker.signals.failed.connect(self._failed)
        self._worker = worker
        self.panel.set_busy(True, "Listening to the clip…")
        w.pool.start(worker)
        return True

    def _found(self, path, highlights) -> None:
        self._worker = None
        w = self.window
        w.project.set_highlights(str(path), highlights)
        w._set_dirty(True)
        current = w.preview.media
        if current is not None and current.path == path:
            self._set(list(highlights))
        if highlights:
            note = f"Found {len(highlights)} moment{'s' if len(highlights) != 1 else ''}."
            if w.transcript.transcript is None:
                note += " Transcribe the clip to also find laughter and shouting."
        else:
            note = "Nothing stood out. Try “More moments”."
        self.panel.set_busy(False, note)

    def _failed(self, error) -> None:
        self._worker = None
        log.warning("Highlight search failed: %s", error)
        self.panel.set_busy(False, f"Search failed: {error}")

    # ------------------------------------------------------------ actions
    def _get(self, index: int):
        return self.highlights[index] if 0 <= index < len(self.highlights) else None

    def _marker_clicked(self, index: int) -> None:
        self.panel.select_index(index)
        self.select(index)
        w = self.window
        w.dock_raise(w.highlights_dock)

    def play(self, index: int) -> None:
        h = self._get(index)
        if h is not None:
            self.window.preview_range(h.start_us + 800_000, h.end_us - 500_000)

    def select(self, index: int) -> None:
        h = self._get(index)
        if h is None:
            return
        w = self.window
        w.timeline.set_range(h.start_us, h.end_us)
        w.preview.seek(h.start_us // 1000)

    def keep(self, index: int) -> None:
        h = self._get(index)
        t = self.window.timeline.timeline
        if h is None or t is None:
            return
        self.window._commit(timeline=t.keep_range(h.start_us, h.end_us))
        self.window.preview.seek(h.start_us // 1000)

"""Glue between the Highlights panel, the timeline and the project:
finding moments, the Twitch chat, ticking moments and saving them as clips."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QFileDialog, QInputDialog, QLineEdit, QMessageBox

from core.audio.waveform import load_waveform
from core.highlights import chat as chat_mod
from core.highlights.clips import save_clips, slice_transcript
from core.highlights.finder import find_highlights
from core.highlights.moments import AUTO, default_length, find_moments, merge_review
from core.render.encoders import choose_encoder
from core.render.settings import RenderSettings
from core.workers import CancellableTask, Worker

if TYPE_CHECKING:
    from ui.main_window.main_window import MainWindow

log = logging.getLogger(__name__)

_CHAT_FILTER = "Chat files (*.json *.txt *.log *.jsonl *.gz);;All files (*)"


class HighlightsController(QObject):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.panel = window.highlights_panel
        self.highlights: list = []
        self.chat: list = []  # chat messages of the current clip
        self.chat_source = ""
        self._worker: Worker | None = None
        self._chat_task: CancellableTask | None = None
        self._save_task: CancellableTask | None = None
        self.panel.find_requested.connect(self.find)
        self.panel.play_requested.connect(self.play)
        self.panel.select_requested.connect(self.select)
        self.panel.keep_requested.connect(self.keep)
        self.panel.accepted_changed.connect(self.set_accepted)
        self.panel.save_requested.connect(self.save_clips)
        self.panel.chat_twitch_requested.connect(self.ask_twitch_chat)
        self.panel.chat_file_requested.connect(self.ask_chat_file)
        self.panel.chat_clear_requested.connect(self.clear_chat)
        view = window.timeline_panel.view
        view.highlight_clicked.connect(self._marker_clicked)

    # ------------------------------------------------------------ state
    def show_for_current(self) -> None:
        """A clip was opened: show its stored highlights and chat."""
        w = self.window
        media = w.preview.media
        stored = w.project.highlights_for(str(media.path)) if media is not None else None
        self._set(list(stored or []))
        self.chat, self.chat_source = [], ""
        if media is not None:
            loaded = chat_mod.load_chat(media.path, w.config.cache_dir)
            if loaded is not None:
                self.chat, self.chat_source = loaded
            timeline = w.timeline.timeline
            if timeline is not None:
                self.panel.suggest_length(default_length(timeline.duration_us))
        self.panel.set_chat_info(len(self.chat), self.chat_source)
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
        length = self.panel.length_s
        chat = list(self.chat)
        previous = list(self.highlights)

        def job():
            waveform = None
            if media.has_audio and ffmpeg is not None:
                waveform = load_waveform(media.path, ffmpeg, cache)
            if length == AUTO:
                found = find_highlights(waveform, transcript, timeline.duration_us, sensitivity, chat=chat)
            else:
                found = find_moments(waveform, transcript, timeline.duration_us, length, sensitivity, chat=chat)
            return merge_review(previous, found)

        worker = Worker(job)
        worker.signals.finished.connect(lambda result, p=media.path: self._found(p, result))
        worker.signals.failed.connect(self._failed)
        self._worker = worker
        long_clip = timeline.duration_us > 20 * 60 * 1_000_000
        self.panel.set_busy(True, "Listening to the recording… (a long stream takes a minute)" if long_clip
                            else "Listening to the clip…")
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
            hints = []
            if w.transcript.transcript is None:
                hints.append("transcribe the clip to also find laughter and shouting")
            if not self.chat:
                hints.append("load the Twitch chat to find what viewers reacted to")
            if hints:
                note += " Tip: " + "; ".join(hints) + "."
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

    def set_accepted(self, index: int, accepted: bool) -> None:
        h = self._get(index)
        media = self.window.preview.media
        if h is None or media is None or h.accepted == accepted:
            return
        self.highlights[index] = replace(h, accepted=accepted)
        self.window.project.set_highlights(str(media.path), self.highlights)
        self.window._set_dirty(True)
        self.window.timeline_panel.view.set_highlights(self.highlights)

    # ------------------------------------------------------------ chat
    def ask_twitch_chat(self) -> None:
        text, ok = QInputDialog.getText(
            self.window, "Chat from Twitch",
            "Link to the stream recording (twitch.tv/videos/…).\n"
            "If your file starts later in the stream, add ?t=1h2m3s to the link.",
            QLineEdit.EchoMode.Normal, "")
        if ok and text.strip():
            link = chat_mod.parse_vod_url(text)
            if link is None:
                QMessageBox.warning(self.window, "Chat from Twitch",
                                    "This is not a link to a stream recording.\n"
                                    "It looks like https://www.twitch.tv/videos/1234567890")
                return
            self.load_twitch_chat(link)

    def load_twitch_chat(self, link, post=None) -> bool:
        w = self.window
        media, timeline = w.preview.media, w.timeline.timeline
        if media is None or timeline is None or self._chat_task is not None:
            return False
        duration_s = timeline.duration_us / 1_000_000
        cache = w.config.cache_dir
        source = f"twitch.tv/videos/{link.video_id}" + (f" from {link.offset_s} s" if link.offset_s else "")

        def job(progress, cancel):
            messages = chat_mod.fetch_vod_chat(link, duration_s, progress=progress, cancel=cancel, post=post)
            chat_mod.save_chat(messages, media.path, cache, source)
            return messages

        return self._run_chat(job, media.path, source, "Downloading the chat…")

    def ask_chat_file(self) -> None:
        w = self.window
        media = w.preview.media
        start = str(media.path.parent) if media is not None else ""
        file, _ = QFileDialog.getOpenFileName(w, "Open chat file", start, _CHAT_FILTER)
        if file:
            self.load_chat_file(Path(file))

    def load_chat_file(self, file: Path) -> bool:
        w = self.window
        media, timeline = w.preview.media, w.timeline.timeline
        if media is None or timeline is None or self._chat_task is not None:
            return False
        cache = w.config.cache_dir
        duration_s = timeline.duration_us / 1_000_000

        def job(progress, cancel):
            messages = chat_mod.shift(chat_mod.read_chat_file(file), 0.0, duration_s + 30)
            if not messages:
                raise chat_mod.ChatError("None of the messages in this file fall inside the video. "
                                         "Is it the chat of this recording?")
            chat_mod.save_chat(messages, media.path, cache, file.name)
            return messages

        return self._run_chat(job, media.path, file.name, "Reading the chat…")

    def _run_chat(self, job, path, source: str, message: str) -> bool:
        task = CancellableTask(job)
        task.signals.progress.connect(lambda _f, text: self.panel.set_chat_busy(True, text))
        task.signals.finished.connect(lambda messages, p=path, s=source: self._chat_loaded(p, messages, s))
        task.signals.failed.connect(self._chat_failed)
        task.signals.cancelled.connect(lambda: self._chat_failed(None))
        self._chat_task = task
        self.panel.set_chat_busy(True, message)
        self.window.pool.start(task)
        return True

    def _chat_loaded(self, path, messages, source: str) -> None:
        self._chat_task = None
        self.panel.set_chat_busy(False)
        media = self.window.preview.media
        if media is not None and media.path == path:
            self.chat, self.chat_source = list(messages), source
            self.panel.set_chat_info(len(self.chat), source)
            self.panel.status.setText("Chat loaded. Press Find highlights to use it.")

    def _chat_failed(self, error) -> None:
        self._chat_task = None
        self.panel.set_chat_busy(False)
        self.panel.set_chat_info(len(self.chat), self.chat_source)
        if error is not None:
            log.warning("Chat failed: %s", error)
            QMessageBox.warning(self.window, "Twitch chat", str(error))

    def clear_chat(self) -> None:
        media = self.window.preview.media
        if media is not None:
            chat_mod.forget_chat(media.path, self.window.config.cache_dir)
        self.chat, self.chat_source = [], ""
        self.panel.set_chat_info(0)

    # ------------------------------------------------------------ clips
    def save_clips(self, folder: Path | None = None) -> bool:
        w = self.window
        if self._save_task is not None:
            self._save_task.cancel()
            return False
        media = w.preview.media
        ticked = [h for h in self.highlights if h.accepted]
        if media is None or not ticked or w.ffmpeg is None:
            return False
        if folder is None:
            chosen = QFileDialog.getExistingDirectory(w, "Save the ticked moments to", str(media.path.parent))
            if not chosen:
                return False
            folder = Path(chosen)
        encoder = choose_encoder(RenderSettings(), w.encoders)
        fps = float((media.video.avg_fps or media.video.fps) if media.video else 30) or 30.0
        ffmpeg = w.ffmpeg.ffmpeg
        task = CancellableTask(save_clips, ffmpeg, media.path, ticked, Path(folder), encoder=encoder, fps=fps)
        task.signals.progress.connect(lambda f, text: self.panel.status.setText(f"{text} {f:.0%}"))
        task.signals.finished.connect(lambda saved, m=media: self._clips_saved(m, saved))
        task.signals.failed.connect(self._clips_failed)
        task.signals.cancelled.connect(lambda: self._clips_failed(None))
        self._save_task = task
        self.panel.save_button.setText("Stop saving")
        self.panel.status.setText("Saving clips…")
        w.pool.start(task)
        return True

    def _clips_done(self) -> None:
        self._save_task = None
        self.panel.save_button.setText("Save ticked as clips…")

    def _clips_saved(self, media, saved) -> None:
        self._clips_done()
        w = self.window
        transcript = w.project.transcript_for(str(media.path))
        files = []
        for path, h in saved:
            part = slice_transcript(transcript, h.start_us, h.end_us)
            if part is not None and part.segments and w.project.transcript_for(str(path)) is None:
                w.project.set_transcript(str(path), part)
            if self.chat:
                try:
                    chat_mod.save_chat(chat_mod.cut(self.chat, h.start_us / 1e6, h.end_us / 1e6), path,
                                       w.config.cache_dir, f"{self.chat_source} (cut)")
                except OSError as exc:
                    log.warning("Could not keep the chat of %s: %s", path, exc)
            files.append(str(path))
        self.panel.status.setText(f"Saved {len(files)} clip{'s' if len(files) != 1 else ''} to "
                                  f"{Path(files[0]).parent if files else ''}. They are in Media.")
        if files:
            w.import_files(files)

    def _clips_failed(self, error) -> None:
        self._clips_done()
        if error is None:
            self.panel.status.setText("Saving stopped.")
            return
        log.warning("Saving clips failed: %s", error)
        self.panel.status.setText(f"Saving failed: {error}")
        QMessageBox.warning(self.window, "Save clips", str(error))

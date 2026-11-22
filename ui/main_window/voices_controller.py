"""Two ways the Metadata tab fills itself, wired to the rest of the window:

* participants by voice: "Find speakers" gives each speaker a voice; voices
  the user named before are recognised and put on the clip as participants
  (core.metadata.voices);
* copy from the previous clip: a new clip of a streamer starts with what that
  streamer's last clip had (core.metadata.carry_over).
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.metadata import carry_over as co
from core.metadata.library import PARTICIPANT, STREAMER
from core.metadata.voices import VoiceBook, speech_seconds

log = logging.getLogger(__name__)


class VoicesController:
    def __init__(self, window) -> None:
        self.window = window
        self.book = VoiceBook(window.config.data_dir / "voices.json")
        panel = window.metadata_panel
        panel.previous_provider = self._previous
        panel.carry_check.setChecked(window.config.metadata_carry_over)
        panel.carry_over_toggled.connect(self._carry_toggled)
        panel.voices_box.find_requested.connect(self._find)
        panel.voices_box.assigned.connect(self._assigned)
        panel.voices_box.create_requested.connect(self._create)

    @property
    def panel(self):
        return self.window.metadata_panel

    @property
    def library(self):
        return self.window.metadata_library

    # --------------------------------------------------------- last clip
    def _carry_toggled(self, on: bool) -> None:
        self.window.config.metadata_carry_over = bool(on)
        try:
            self.window.config.save()
        except OSError:
            pass

    def _previous(self, metadata, streamer_id: str):
        return co.from_last_clip(metadata, self.window.clip_memory, self.library, self.window._current_source(),
                                 streamer_id)

    def fill_new_clip(self, source: str) -> bool:
        """A clip the app has not seen is opened: fill it from its streamer's last clip.

        The streamer is the one already chosen, else the one named in the file name, else the
        streamer of the clip edited last (the user usually cuts one stream's clips in a row)."""
        panel, w = self.panel, self.window
        if not source or not panel.carry_check.isChecked():
            return False
        current = panel.metadata()
        streamer = current.selection.streamer_id or co.guess_streamer(self.library, Path(source).name)
        if not streamer:
            last = co.last_clip_of(w.clip_memory, exclude=source)
            streamer = last.metadata.selection.streamer_id if last is not None else ""
        if not streamer or self.library.person(streamer, STREAMER) is None:
            return False
        return panel.fill_from_last_clip(streamer)

    # ------------------------------------------------------------ voices
    def _people(self):
        return self.library.all_people(PARTICIPANT)

    def show(self, source: str = "") -> None:
        source = source or self.window._current_source()
        self.panel.set_voices(self.book.get(source) if source else None, self._people())

    def _find(self) -> None:
        w = self.window
        if w.preview.media is None:
            self.panel.status_label.setText("Open a clip first.")
            return
        if w.transcript.transcript is None:
            self.panel.status_label.setText("Transcribe the clip first (Transcript), then find voices.")
            return
        if w._transcribe_task is not None:
            self.panel.status_label.setText("Wait for the transcript work to finish.")
            return
        self.panel.status_label.setText("Listening for voices…")
        w.find_speakers(None)

    def speakers_found(self, source: str, transcript, labels) -> list[str]:
        """After "Find speakers": keep the voices, recognise people, put them on the clip.
        Returns the names of the people put on the clip."""
        voices = getattr(labels, "voices", None)
        if not voices:
            return []
        allowed = {p.id for p in self._people()}
        clip, matches = self.book.set_clip(source, voices, speech_seconds(transcript), allowed)
        added = []
        for m in matches:
            if self.panel.add_person(m.person_id):
                person = self.library.person(m.person_id, PARTICIPANT)
                added.append(person.name if person else m.person_id)
        self.show(source)
        if added:
            self.panel.status_label.setText("Recognised by voice: " + ", ".join(added) + ".")
        elif clip.voices:
            self.panel.status_label.setText("Voices found. Name them under Voices so the next clips "
                                            "recognise them.")
        return added

    def _assigned(self, speaker: int, person_id: str) -> None:
        source = self.window._current_source()
        if not source or not self.book.assign(source, speaker, person_id):
            return
        if person_id:
            self.panel.add_person(person_id)
            person = self.library.person(person_id, PARTICIPANT)
            if person is not None:
                self.panel.status_label.setText(
                    f"Speaker {speaker + 1} is {person.name}: the next clips will recognise this voice.")
        self.show(source)

    def _create(self, speaker: int, name: str) -> None:
        person = self.library.create(PARTICIPANT, name, username=name.lstrip("@"))
        self.panel.refresh_library()
        self._assigned(speaker, person.id)

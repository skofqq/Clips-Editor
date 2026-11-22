"""Voices: who the speakers of a clip are, recognised by their voice.

"Find speakers" groups a clip's words by voice and gives each group an average
voice vector (WeSpeaker CAM++ embedding, unit length). This store keeps, per
clip, those vectors and which library person each speaker is. A person's
voiceprint is the average of every clip voice the user said was them, so
picking "Speaker 2 = Mira" once lets the next clip recognise Mira by herself;
undoing that choice also takes the voice back out of her voiceprint.

Voices that were only recognised (not picked by the user) are remembered for
the clip but do not teach the voiceprint, so a wrong guess cannot snowball.

Stored as one JSON file in the user data folder. Nothing leaves the computer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

VERSION = 1
MATCH = 0.5  # cosine similarity a voice needs to be recognised as a person
MIN_WINDOWS = 3  # ~3 s of speech: shorter voices are too unsure to recognise or learn from


def clip_key(path: str | Path) -> str:
    return str(Path(path)).replace("\\", "/").casefold()


def _unit(vec) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32).ravel()
    return v / (float(np.linalg.norm(v)) + 1e-9)


@dataclass
class Voice:
    """One speaker of one clip."""

    vector: np.ndarray
    windows: int = 0  # 0.75 s steps the speaker was heard in
    seconds: float = 0.0  # speech in the transcript
    person_id: str = ""
    auto: bool = False  # recognised, not picked by the user
    similarity: float = 0.0  # of the recognition

    def to_dict(self) -> dict:
        return {"vector": [round(float(x), 5) for x in self.vector], "windows": self.windows,
                "seconds": round(self.seconds, 2), "person_id": self.person_id, "auto": self.auto,
                "similarity": round(self.similarity, 3)}

    @classmethod
    def from_dict(cls, data: dict) -> "Voice":
        return cls(_unit(data.get("vector") or [0.0]), int(data.get("windows") or 0),
                   float(data.get("seconds") or 0.0), str(data.get("person_id") or ""), bool(data.get("auto")),
                   float(data.get("similarity") or 0.0))


@dataclass(frozen=True)
class Match:
    speaker: int
    person_id: str
    similarity: float


@dataclass
class ClipVoices:
    source: str
    voices: dict[int, Voice] = field(default_factory=dict)

    def people(self) -> dict[int, str]:
        return {k: v.person_id for k, v in self.voices.items() if v.person_id}


class VoiceBook:
    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path is not None else None
        self._clips: dict[str, ClipVoices] = {}
        self._prints: dict[str, np.ndarray] | None = None
        self._load()

    # ------------------------------------------------------------ file
    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Could not read the voices: %s", exc)
            return
        for item in (raw.get("clips") or []) if isinstance(raw, dict) else []:
            try:
                voices = {int(k): Voice.from_dict(v) for k, v in (item.get("voices") or {}).items()}
            except (TypeError, ValueError, AttributeError):
                continue
            source = str(item.get("source") or "")
            if source:
                self._clips[clip_key(source)] = ClipVoices(source, voices)

    def save(self) -> None:
        if self.path is None:
            return
        data = {"version": VERSION, "clips": [
            {"source": c.source, "voices": {str(k): v.to_dict() for k, v in c.voices.items()}}
            for c in self._clips.values()]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("Could not save the voices: %s", exc)

    # ------------------------------------------------------------ voiceprints
    def voiceprints(self) -> dict[str, np.ndarray]:
        """person id → average of the voices the user said were them."""
        if self._prints is None:
            sums: dict[str, np.ndarray] = {}
            for clip in self._clips.values():
                for voice in clip.voices.values():
                    if voice.person_id and not voice.auto and voice.windows >= MIN_WINDOWS:
                        weight = min(voice.windows, 40)  # one long clip should not drown the others
                        sums[voice.person_id] = sums.get(voice.person_id, 0) + voice.vector * weight
            self._prints = {pid: _unit(v) for pid, v in sums.items()}
        return self._prints

    def known_people(self) -> set[str]:
        return set(self.voiceprints())

    def match(self, voices: dict[int, Voice], allowed: set[str] | None = None,
              threshold: float = MATCH) -> list[Match]:
        """Best person per speaker, each person at most once, most similar pairs first.
        ``allowed``: person ids that still exist (None = all)."""
        prints = {pid: v for pid, v in self.voiceprints().items() if allowed is None or pid in allowed}
        pairs = []
        for speaker, voice in voices.items():
            if voice.windows < MIN_WINDOWS:
                continue
            for pid, vec in prints.items():
                sim = float(np.dot(voice.vector, vec))
                if sim >= threshold:
                    pairs.append((sim, speaker, pid))
        result, used_speakers, used_people = [], set(), set()
        for sim, speaker, pid in sorted(pairs, reverse=True):
            if speaker in used_speakers or pid in used_people:
                continue
            used_speakers.add(speaker)
            used_people.add(pid)
            result.append(Match(speaker, pid, sim))
        return sorted(result, key=lambda m: m.speaker)

    # ------------------------------------------------------------ clips
    def get(self, source: str | Path) -> ClipVoices | None:
        return self._clips.get(clip_key(source))

    def set_clip(self, source: str | Path, voices: dict, seconds: dict[int, float] | None = None,
                 allowed: set[str] | None = None) -> tuple[ClipVoices, list[Match]]:
        """New "Find speakers" result for a clip: forget its old speakers, recognise the new ones.

        ``voices`` is speaker → (vector, windows) from diarization."""
        seconds = seconds or {}
        clip = ClipVoices(str(source), {
            int(k): Voice(_unit(vec), int(windows), float(seconds.get(int(k), 0.0)))
            for k, (vec, windows) in voices.items()})
        old = self._clips.pop(clip_key(source), None)
        if old is not None and old.people() and any(not v.auto for v in old.voices.values()):
            self._prints = None
        matches = self.match(clip.voices, allowed)
        for m in matches:
            voice = clip.voices[m.speaker]
            voice.person_id, voice.auto, voice.similarity = m.person_id, True, m.similarity
        self._clips[clip_key(source)] = clip
        self.save()
        return clip, matches

    def assign(self, source: str | Path, speaker: int, person_id: str) -> bool:
        """The user says ``speaker`` of the clip is ``person_id`` ("" = nobody). Teaches the voiceprint."""
        clip = self.get(source)
        if clip is None or speaker not in clip.voices:
            return False
        if person_id:  # one person is one speaker of a clip
            for k, voice in clip.voices.items():
                if k != speaker and voice.person_id == person_id:
                    voice.person_id, voice.auto = "", False
        voice = clip.voices[speaker]
        voice.person_id, voice.auto, voice.similarity = person_id, False, 0.0
        self._prints = None
        self.save()
        return True

    def forget_person(self, person_id: str) -> None:
        """A profile was deleted: drop it from every clip."""
        changed = False
        for clip in self._clips.values():
            for voice in clip.voices.values():
                if voice.person_id == person_id:
                    voice.person_id, voice.auto = "", False
                    changed = True
        if changed:
            self._prints = None
            self.save()


def speech_seconds(transcript) -> dict[int, float]:
    """How long each speaker talks in a transcript (core.transcription.model.Transcript)."""
    result: dict[int, float] = {}
    for w in transcript.words():
        if w.speaker >= 0:
            result[w.speaker] = result.get(w.speaker, 0.0) + max(0, w.end_us - w.start_us) / 1e6
    return result

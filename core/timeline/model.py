"""Non-destructive edit model for one source clip.

Everything is expressed in **source time** (integer microseconds):

* ``segments`` are the kept ranges of the source, in order. Trimming,
  splitting and deleting only change this list; the file is never touched.
* ``mutes`` are audio ranges to silence or beep. Being in source time,
  they stay attached to the same words when the clip is trimmed, and
  transcription / censor results (Phases 4-5) map onto them directly.
* ``gain_db`` and the fades apply to the whole output.
* a segment may carry its own Stacked ``layout`` (a Composition) for when
  the streamer moved the webcam mid-clip; None means the project's layout.

The model is immutable: every operation returns a new :class:`Timeline`,
which makes undo/redo a matter of keeping old values.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

MIN_SEGMENT_US = 40_000
MIN_MUTE_US = 20_000
MIN_GAIN_DB, MAX_GAIN_DB = -40.0, 20.0

SILENCE = "silence"
BEEP = "beep"
MUTE_MODES = (SILENCE, BEEP)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


@dataclass(frozen=True)
class Segment:
    start_us: int
    end_us: int
    layout: Any = None  # Composition for this part, or None = the project's layout

    @property
    def length_us(self) -> int:
        return self.end_us - self.start_us

    def contains(self, us: int) -> bool:
        return self.start_us <= us < self.end_us


@dataclass(frozen=True)
class MuteRegion:
    id: int
    start_us: int
    end_us: int
    mode: str = SILENCE

    @property
    def length_us(self) -> int:
        return self.end_us - self.start_us


@dataclass(frozen=True)
class Timeline:
    duration_us: int
    segments: tuple[Segment, ...]
    mutes: tuple[MuteRegion, ...] = ()
    gain_db: float = 0.0
    fade_in_us: int = 0
    fade_out_us: int = 0
    next_id: int = 1

    @classmethod
    def for_source(cls, duration_us: int) -> "Timeline":
        duration_us = max(MIN_SEGMENT_US, int(duration_us))
        return cls(duration_us=duration_us, segments=(Segment(0, duration_us),))

    # ------------------------------------------------------------ queries
    @property
    def output_us(self) -> int:
        """Length of the edited result."""
        return sum(s.length_us for s in self.segments)

    @property
    def start_us(self) -> int:
        return self.segments[0].start_us

    @property
    def end_us(self) -> int:
        return self.segments[-1].end_us

    def segment_index_at(self, us: int) -> int | None:
        for index, segment in enumerate(self.segments):
            if segment.contains(us):
                return index
        return None

    def next_kept(self, us: int) -> int | None:
        """``us`` if it is inside a kept segment, else the start of the next
        segment, else None (past the end of the edit)."""
        for segment in self.segments:
            if us < segment.start_us:
                return segment.start_us
            if us < segment.end_us:
                return us
        return None

    def source_to_output(self, us: int) -> int:
        """Output time of a source position (positions in gaps map to the cut)."""
        total = 0
        for segment in self.segments:
            if us <= segment.start_us:
                break
            total += min(us, segment.end_us) - segment.start_us
        return total

    def output_to_source(self, out_us: int) -> int:
        remaining = max(0, int(out_us))
        for segment in self.segments:
            if remaining < segment.length_us:
                return segment.start_us + remaining
            remaining -= segment.length_us
        return self.end_us

    def mute(self, mute_id: int) -> MuteRegion | None:
        return next((m for m in self.mutes if m.id == mute_id), None)

    def mute_at(self, us: int) -> MuteRegion | None:
        return next((m for m in self.mutes if m.start_us <= us < m.end_us), None)

    def merged_mutes(self) -> list[tuple[int, int, str]]:
        """Mute ranges with overlapping/adjacent ones of the same mode merged.

        The stored regions stay separate so each can still be edited; this
        is the form used for preview and rendering.
        """
        result: list[tuple[int, int, str]] = []
        for mode in MUTE_MODES:
            current: list[int] | None = None
            for m in sorted((m for m in self.mutes if m.mode == mode), key=lambda m: m.start_us):
                if current is not None and m.start_us <= current[1]:
                    current[1] = max(current[1], m.end_us)
                else:
                    if current is not None:
                        result.append((current[0], current[1], mode))
                    current = [m.start_us, m.end_us]
            if current is not None:
                result.append((current[0], current[1], mode))
        return sorted(result)

    @property
    def gain_linear(self) -> float:
        return 10 ** (self.gain_db / 20)

    def fade_factor(self, us: int) -> float:
        """0..1 multiplier from Fade In / Fade Out at a source position."""
        out = self.source_to_output(us)
        factor = 1.0
        if self.fade_in_us > 0 and out < self.fade_in_us:
            factor = min(factor, out / self.fade_in_us)
        remaining = self.output_us - out
        if self.fade_out_us > 0 and remaining < self.fade_out_us:
            factor = min(factor, remaining / self.fade_out_us)
        return max(0.0, factor)

    def volume_at(self, us: int) -> float:
        """Linear audio multiplier at a source position (0 when muted or cut)."""
        if self.segment_index_at(us) is None or self.mute_at(us) is not None:
            return 0.0
        return self.gain_linear * self.fade_factor(us)

    # ----------------------------------------------------------- segments
    def split_at(self, us: int) -> "Timeline":
        index = self.segment_index_at(us)
        if index is None:
            return self
        segment = self.segments[index]
        if us - segment.start_us < MIN_SEGMENT_US or segment.end_us - us < MIN_SEGMENT_US:
            return self
        parts = (replace(segment, end_us=us), replace(segment, start_us=us))
        return replace(self, segments=self.segments[:index] + parts + self.segments[index + 1:])

    def trim_segment(self, index: int, start_us: int | None = None, end_us: int | None = None) -> "Timeline":
        """Move a segment's edges; they cannot cross neighbours or the source."""
        if not 0 <= index < len(self.segments):
            return self
        segment = self.segments[index]
        low = self.segments[index - 1].end_us if index > 0 else 0
        high = self.segments[index + 1].start_us if index + 1 < len(self.segments) else self.duration_us
        start, end = segment.start_us, segment.end_us
        if start_us is not None:
            start = _clamp(start_us, low, end - MIN_SEGMENT_US)
        if end_us is not None:
            end = _clamp(end_us, start + MIN_SEGMENT_US, high)
        updated = replace(segment, start_us=start, end_us=end)
        if updated == segment:
            return self
        return replace(self, segments=self.segments[:index] + (updated,) + self.segments[index + 1:])

    def delete_segment(self, index: int) -> "Timeline":
        if len(self.segments) <= 1 or not 0 <= index < len(self.segments):
            return self
        return replace(self, segments=self.segments[:index] + self.segments[index + 1:])

    def delete_range(self, start_us: int, end_us: int) -> "Timeline":
        """Cut a source range out of the kept segments."""
        start_us, end_us = sorted((int(start_us), int(end_us)))
        kept: list[Segment] = []
        for segment in self.segments:
            if end_us <= segment.start_us or start_us >= segment.end_us:
                kept.append(segment)
                continue
            if start_us - segment.start_us >= MIN_SEGMENT_US:
                kept.append(replace(segment, end_us=start_us))
            if segment.end_us - end_us >= MIN_SEGMENT_US:
                kept.append(replace(segment, start_us=end_us))
        if not kept or tuple(kept) == self.segments:
            return self
        return replace(self, segments=tuple(kept))

    def keep_range(self, start_us: int, end_us: int) -> "Timeline":
        """Trim to a range: drop everything outside it."""
        start_us, end_us = sorted((int(start_us), int(end_us)))
        result = self.delete_range(end_us, self.duration_us) if end_us < self.duration_us else self
        return result.delete_range(0, start_us) if start_us > 0 else result

    def restore_all(self) -> "Timeline":
        return replace(self, segments=(Segment(0, self.duration_us),))

    # ------------------------------------------------------------- layouts
    def layout_index_at(self, us: int) -> int:
        """The part whose layout applies at source time ``us``: the part under
        it, else the next one, else the last."""
        index = self.segment_index_at(us)
        if index is not None:
            return index
        for i, segment in enumerate(self.segments):
            if us < segment.start_us:
                return i
        return len(self.segments) - 1

    def with_segment_layout(self, index: int, layout) -> "Timeline":
        if not 0 <= index < len(self.segments) or self.segments[index].layout == layout:
            return self
        segments = list(self.segments)
        segments[index] = replace(segments[index], layout=layout)
        return replace(self, segments=tuple(segments))

    def without_layouts(self) -> "Timeline":
        if not self.has_layouts:
            return self
        return replace(self, segments=tuple(replace(s, layout=None) for s in self.segments))

    @property
    def has_layouts(self) -> bool:
        return any(s.layout is not None for s in self.segments)

    # -------------------------------------------------------------- mutes
    def add_mute(self, start_us: int, end_us: int, mode: str = SILENCE) -> "Timeline":
        start_us, end_us = sorted((int(start_us), int(end_us)))
        start = _clamp(start_us, 0, self.duration_us)
        end = _clamp(end_us, 0, self.duration_us)
        if end - start < MIN_MUTE_US or mode not in MUTE_MODES:
            return self
        region = MuteRegion(self.next_id, start, end, mode)
        mutes = tuple(sorted(self.mutes + (region,), key=lambda m: (m.start_us, m.id)))
        return replace(self, mutes=mutes, next_id=self.next_id + 1)

    def update_mute(
        self, mute_id: int, start_us: int | None = None, end_us: int | None = None, mode: str | None = None
    ) -> "Timeline":
        region = self.mute(mute_id)
        if region is None:
            return self
        start, end = region.start_us, region.end_us
        if start_us is not None:
            start = _clamp(start_us, 0, end - MIN_MUTE_US)
        if end_us is not None:
            end = _clamp(end_us, start + MIN_MUTE_US, self.duration_us)
        updated = MuteRegion(mute_id, start, end, mode if mode in MUTE_MODES else region.mode)
        if updated == region:
            return self
        mutes = tuple(sorted((updated if m.id == mute_id else m for m in self.mutes), key=lambda m: (m.start_us, m.id)))
        return replace(self, mutes=mutes)

    def move_mute(self, mute_id: int, start_us: int) -> "Timeline":
        """Move a region keeping its length, clamped to the source."""
        region = self.mute(mute_id)
        if region is None:
            return self
        start = _clamp(start_us, 0, self.duration_us - region.length_us)
        if start == region.start_us:
            return self
        moved = replace(region, start_us=start, end_us=start + region.length_us)
        mutes = tuple(sorted((moved if m.id == mute_id else m for m in self.mutes), key=lambda m: (m.start_us, m.id)))
        return replace(self, mutes=mutes)

    def delete_mute(self, mute_id: int) -> "Timeline":
        mutes = tuple(m for m in self.mutes if m.id != mute_id)
        return self if len(mutes) == len(self.mutes) else replace(self, mutes=mutes)

    def merge_mutes(self) -> "Timeline":
        """Replace the stored regions with their merged form."""
        merged = self.merged_mutes()
        if len(merged) == len(self.mutes):
            return self
        next_id = self.next_id
        regions = []
        for start, end, mode in merged:
            regions.append(MuteRegion(next_id, start, end, mode))
            next_id += 1
        return replace(self, mutes=tuple(regions), next_id=next_id)

    # -------------------------------------------------------------- audio
    def with_gain_db(self, gain_db: float) -> "Timeline":
        gain_db = round(max(MIN_GAIN_DB, min(MAX_GAIN_DB, float(gain_db))), 2)
        return self if gain_db == self.gain_db else replace(self, gain_db=gain_db)

    def with_fades(self, fade_in_us: int | None = None, fade_out_us: int | None = None) -> "Timeline":
        limit = self.output_us
        fade_in = self.fade_in_us if fade_in_us is None else _clamp(fade_in_us, 0, limit)
        fade_out = self.fade_out_us if fade_out_us is None else _clamp(fade_out_us, 0, limit)
        if (fade_in, fade_out) == (self.fade_in_us, self.fade_out_us):
            return self
        return replace(self, fade_in_us=fade_in, fade_out_us=fade_out)

    # ------------------------------------------------------ serialisation
    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_us": self.duration_us,
            "segments": [
                [s.start_us, s.end_us] + ([s.layout.to_dict()] if s.layout is not None else []) for s in self.segments
            ],
            "mutes": [
                {"id": m.id, "start_us": m.start_us, "end_us": m.end_us, "mode": m.mode}
                for m in self.mutes
            ],
            "gain_db": self.gain_db,
            "fade_in_us": self.fade_in_us,
            "fade_out_us": self.fade_out_us,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Timeline":
        if not isinstance(data, dict):
            raise ValueError("Timeline must be an object.")
        duration = int(data["duration_us"])
        if duration < MIN_SEGMENT_US:
            raise ValueError("Timeline duration is too short.")
        segments: list[Segment] = []
        previous_end = 0
        for item in data.get("segments") or []:
            start, end = int(item[0]), int(item[1])
            if start < previous_end or end - start < 1 or end > duration:
                raise ValueError("Timeline segments are out of order or out of range.")
            layout = None
            if len(item) > 2 and isinstance(item[2], dict):
                from core.editor.composition import Composition

                layout = Composition.from_dict(item[2])
            segments.append(Segment(start, end, layout))
            previous_end = end
        if not segments:
            raise ValueError("Timeline has no segments.")
        mutes: list[MuteRegion] = []
        seen: set[int] = set()
        for item in data.get("mutes") or []:
            mute_id, start, end = int(item["id"]), int(item["start_us"]), int(item["end_us"])
            mode = item.get("mode", SILENCE)
            if mute_id in seen or mute_id < 1 or not 0 <= start < end <= duration or mode not in MUTE_MODES:
                raise ValueError("Invalid mute region.")
            seen.add(mute_id)
            mutes.append(MuteRegion(mute_id, start, end, mode))
        mutes.sort(key=lambda m: (m.start_us, m.id))
        timeline = cls(
            duration_us=duration,
            segments=tuple(segments),
            mutes=tuple(mutes),
            next_id=max(seen, default=0) + 1,
        )
        timeline = timeline.with_gain_db(float(data.get("gain_db", 0.0)))
        return timeline.with_fades(int(data.get("fade_in_us", 0)), int(data.get("fade_out_us", 0)))

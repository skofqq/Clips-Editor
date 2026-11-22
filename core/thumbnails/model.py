"""What a thumbnail looks like: which frame, the text and its style.

Stored in the project. The picture itself is drawn on demand
(:mod:`core.thumbnails.draw`), so changing the edit never leaves a stale file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from typing import Any

VERTICAL, LANDSCAPE = "vertical", "landscape"
SIZES = {VERTICAL: (1080, 1920), LANDSCAPE: (1280, 720)}
FORMAT_TEXT = {VERTICAL: "Vertical 1080 × 1920 (Shorts)", LANDSCAPE: "Landscape 1280 × 720"}

TOP, MIDDLE, BOTTOM = "top", "middle", "bottom"
POSITIONS = (TOP, MIDDLE, BOTTOM)
POSITION_TEXT = {TOP: "Top", MIDDLE: "Middle", BOTTOM: "Bottom"}


@dataclass(frozen=True)
class TextStyle:
    name: str
    color: str
    outline: str = "#000000"
    box: str = ""  # a colour = text on a rounded box


STYLES: dict[str, TextStyle] = {
    "yellow": TextStyle("Yellow, black outline", "#ffe600"),
    "white": TextStyle("White, black outline", "#ffffff"),
    "red_box": TextStyle("White on red", "#ffffff", "", "#e5262b"),
    "purple_box": TextStyle("White on Twitch purple", "#ffffff", "", "#9146ff"),
    "black_box": TextStyle("Yellow on black", "#ffe600", "", "#000000d0"),
    "green": TextStyle("Green, black outline", "#39ff14"),
}

MIN_SIZE, MAX_SIZE = 50, 260  # text height in px of a 1080-wide picture
MIN_ZOOM, MAX_ZOOM = 1.0, 2.0
MAX_OFFSET = 45  # text shift up (-) or down (+), % of the picture height


@dataclass(frozen=True)
class ThumbnailDesign:
    enabled: bool = True
    source_us: int | None = None  # the frame (source time); None = a quarter into the clip
    text: str = ""  # empty = the clip title
    style: str = "yellow"
    position: str = TOP
    offset: int = 0  # move the text down (+) or up (-) from its position, % of the height
    size: int = 140
    uppercase: bool = True
    shade: bool = True  # darken behind the text so it reads on any frame
    zoom: float = 1.0  # punch in on the frame's centre (on the face when one is known)
    format: str = VERTICAL
    face: tuple[float, float, float, float] | None = None  # streamer's face in the source frame (0..1 box)
    face_cutout: bool = False  # draw the face again, big, as a framed cut-out

    @property
    def text_style(self) -> TextStyle:
        return STYLES.get(self.style, STYLES["yellow"])

    @property
    def output_size(self) -> tuple[int, int]:
        return SIZES.get(self.format, SIZES[VERTICAL])

    def shown_text(self, title: str) -> str:
        text = (self.text or "").strip() or clip_title(title)
        return text.upper() if self.uppercase else text

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v != getattr(DEFAULT, k)}

    @classmethod
    def from_dict(cls, data: Any) -> "ThumbnailDesign":
        if not isinstance(data, dict):
            return cls()
        names = {f.name for f in fields(cls)}
        design = cls(**{k: v for k, v in data.items() if k in names})
        return design.clean()

    def clean(self) -> "ThumbnailDesign":
        source = None if self.source_us is None else max(0, int(self.source_us))
        face = _clean_face(self.face)
        return replace(
            self,
            enabled=bool(self.enabled),
            source_us=source,
            text=str(self.text or ""),
            style=self.style if self.style in STYLES else "yellow",
            position=self.position if self.position in POSITIONS else TOP,
            offset=max(-MAX_OFFSET, min(MAX_OFFSET, int(self.offset))),
            size=max(MIN_SIZE, min(MAX_SIZE, int(self.size))),
            uppercase=bool(self.uppercase),
            shade=bool(self.shade),
            zoom=max(MIN_ZOOM, min(MAX_ZOOM, float(self.zoom))),
            format=self.format if self.format in SIZES else VERTICAL,
            face=face,
            face_cutout=bool(self.face_cutout) and face is not None,
        )


def _clean_face(face) -> tuple[float, float, float, float] | None:
    try:
        x1, y1, x2, y2 = (min(1.0, max(0.0, float(v))) for v in face)
    except (TypeError, ValueError):
        return None
    if x2 - x1 < 0.002 or y2 - y1 < 0.002:
        return None
    return (round(x1, 5), round(y1, 5), round(x2, 5), round(y2, 5))


DEFAULT = ThumbnailDesign()


def clip_title(title: str) -> str:
    """The clip's own name from a YouTube title: "КОНСТРУКТИВНАЯ ОХОТА | #sasavot on #Twitch"
    gives "КОНСТРУКТИВНАЯ ОХОТА" (hashtags and the channel part do not belong on a cover)."""
    text = (title or "").split("|")[0]
    words = [w for w in text.split() if not w.startswith("#")]
    return " ".join(words).strip()

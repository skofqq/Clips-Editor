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
    zoom: float = 1.0  # punch in on the frame's centre
    format: str = VERTICAL

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
        )


DEFAULT = ThumbnailDesign()


def clip_title(title: str) -> str:
    """The clip's own name from a YouTube title: "КОНСТРУКТИВНАЯ ОХОТА | #sasavot on #Twitch"
    gives "КОНСТРУКТИВНАЯ ОХОТА" (hashtags and the channel part do not belong on a cover)."""
    text = (title or "").split("|")[0]
    words = [w for w in text.split() if not w.startswith("#")]
    return " ".join(words).strip()

"""Several covers to choose from: the best suggested frames, each with the
current text style and a contrasting one."""

from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from core.thumbnails.draw import draw_thumbnail
from core.thumbnails.model import STYLES, ThumbnailDesign

# The style tried next to the current one: a box when the text is plain, plain when it has a box.
CONTRAST = {"yellow": "purple_box", "white": "red_box", "green": "black_box",
            "red_box": "yellow", "purple_box": "yellow", "black_box": "white"}


@dataclass(frozen=True)
class CoverVariant:
    label: str
    design: ThumbnailDesign


def cover_variants(design: ThumbnailDesign, frame_times: list[int], frames: int = 3,
                   faces: dict | None = None) -> list[CoverVariant]:
    """Frames × (current style, contrasting style); the frame number is its place among the suggestions.
    ``faces``: the streamer's face found on a frame (source µs → 0..1 box) by Auto cover."""
    styles = [design.style, CONTRAST.get(design.style, "yellow")]
    faces = faces or {}
    result = []
    for n, us in enumerate(frame_times[:frames], 1):
        face = faces.get(int(us))
        cutout = face is not None and (design.face_cutout or design.face is None)
        for style in styles:
            result.append(CoverVariant(f"Frame {n} · {STYLES[style].name}",
                                       replace(design, source_us=int(us), style=style, face=face,
                                               face_cutout=cutout).clean()))
    return result


def render_variants(variants: list[CoverVariant], frames: dict[int, QImage], title: str,
                    height: int = 240, face_boxes: dict | None = None) -> list[QImage]:
    """Small pictures of the variants, drawn from the suggested frames (runs off the GUI thread).
    ``face_boxes``: source µs → (face, webcam area) as 0..1 boxes of the composed frame."""
    images = []
    for v in variants:
        frame = frames.get(v.design.source_us)
        if frame is None or frame.isNull():
            images.append(QImage())
            continue
        found = (face_boxes or {}).get(v.design.source_us) if v.design.face is not None else None
        box, area = found if found is not None else (None, None)
        big = draw_thumbnail(frame, v.design, title, box, area)
        images.append(big.scaledToHeight(height, Qt.TransformationMode.SmoothTransformation))
    return images

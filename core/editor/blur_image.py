"""Blur boxes in the preview: the same blur / pixelate and soft edge the
render applies (core.render.graph.blur_lines), painted with Qt."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QPainter

from core.editor.blur import PIXELATE, BlurRegion


@lru_cache(maxsize=32)
def feather_mask(w: int, h: int, feather: float) -> QImage:
    """Alpha mask: opaque inside, fading to clear over ``feather`` px at the edges
    (the same linear ramp as the render's geq mask)."""
    xs = np.arange(w, dtype=np.float32) + 0.5
    ys = np.arange(h, dtype=np.float32) + 0.5
    dx = np.minimum(xs, w - xs)
    dy = np.minimum(ys, h - ys)
    dist = np.minimum(dy[:, None], dx[None, :])
    alpha = np.clip(dist / max(feather, 1e-3), 0.0, 1.0) if feather >= 1 else np.ones((h, w), np.float32)
    data = np.ascontiguousarray((alpha * 255).astype(np.uint8))
    image = QImage(data.data, w, h, w, QImage.Format.Format_Alpha8)
    return image.copy()  # own the pixels


def _blurred(patch: QImage, region: BlurRegion, scale: float, src_h: int) -> QImage:
    w, h = patch.width(), patch.height()
    if region.mode == PIXELATE:
        block = max(1.0, region.block_px(src_h) * scale)
        small = patch.scaled(max(1, int(w / block)), max(1, int(h / block)),
                             Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        return small.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
    # Gaussian blur approximated by shrinking and smoothly growing back, twice.
    sigma = max(0.5, region.sigma_px(src_h) * scale)
    factor = max(1.0, sigma * 1.2)
    result = patch
    for _ in range(2):
        small = result.scaled(max(1, int(w / factor)), max(1, int(h / factor)),
                              Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        result = small.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return result


def apply_blurs(image: QImage, blurs, t_us: int, src_w: int, src_h: int) -> QImage:
    """``image`` with the boxes active at source time ``t_us`` blurred. The
    frame may be decoded at another size than ``src_w``×``src_h``."""
    active = [b for b in blurs if b.active(t_us)]
    if image.isNull() or not active or src_w <= 0 or src_h <= 0:
        return image
    out = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    kx, ky = out.width() / src_w, out.height() / src_h
    for region in active:
        r = region.rect_at(t_us)
        box = QRect(int(round(r.x * out.width())), int(round(r.y * out.height())),
                    max(2, int(round(r.w * out.width()))), max(2, int(round(r.h * out.height()))))
        box = box.intersected(out.rect())
        if box.width() < 2 or box.height() < 2:
            continue
        patch = _blurred(out.copy(box), region, (kx + ky) / 2, src_h)
        feather = region.feather_px(box.width(), box.height())
        if feather >= 1:
            patch = patch.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            masker = QPainter(patch)
            masker.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            masker.drawImage(0, 0, feather_mask(box.width(), box.height(), round(feather, 1)))
            masker.end()
        painter = QPainter(out)
        painter.drawImage(box.topLeft(), patch)
        painter.end()
    return out

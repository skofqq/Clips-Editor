from __future__ import annotations

from dataclasses import replace

import pytest

from core.editor.composition import (
    GAMEPLAY,
    WEBCAM,
    Composition,
    FitMode,
    Region,
    default_composition,
    match_slot_aspect,
)
from core.editor.geometry import Rect
from core.editor.presets import PresetStore
from core.projects.project import Project, ProjectFormatError, load_project, save_project

SRC = (1920, 1080)


def placements(comp, src=SRC):
    return {p.name: p for p in comp.layout(*src)}


def approx_rect(rect: Rect, x, y, w, h, tol=0.01):
    assert rect.x == pytest.approx(x, abs=tol)
    assert rect.y == pytest.approx(y, abs=tol)
    assert rect.w == pytest.approx(w, abs=tol)
    assert rect.h == pytest.approx(h, abs=tol)


def test_default_slots_stack_webcam_over_gameplay():
    comp = Composition()
    slots = comp.slots()
    approx_rect(slots[WEBCAM], 0, 0, 1080, 672)
    approx_rect(slots[GAMEPLAY], 0, 672, 1080, 1248)
    # Slots tile the canvas exactly.
    assert slots[WEBCAM].h + slots[GAMEPLAY].h == 1920


def test_swap_and_divider():
    comp = Composition().with_swapped().with_divider(0.5)
    slots = comp.slots()
    assert slots[GAMEPLAY].y == 0 and slots[GAMEPLAY].h == 960
    assert slots[WEBCAM].y == 960
    assert Composition().with_divider(5).divider == 0.9
    assert Composition().with_divider(-1).divider == 0.1


def test_disabled_region_gives_full_canvas_to_the_other():
    comp = Composition()
    comp = comp.with_region(WEBCAM, replace(comp.webcam, enabled=False))
    slots = comp.slots()
    assert list(slots) == [GAMEPLAY]
    approx_rect(slots[GAMEPLAY], 0, 0, 1080, 1920)
    assert [p.name for p in comp.layout(*SRC)] == [GAMEPLAY]
    both_off = comp.with_region(GAMEPLAY, replace(comp.gameplay, enabled=False))
    assert both_off.layout(*SRC) == []


def test_fill_crops_overflow_and_keeps_aspect():
    # 960x540 crop (16:9) into a 1080x672 slot: fill scales by height.
    comp = Composition().with_region(WEBCAM, Region(Rect(0, 0, 0.5, 0.5)))
    p = placements(comp)[WEBCAM]
    approx_rect(p.dst, 0, 0, 1080, 672)  # slot fully covered
    assert p.src.h == pytest.approx(540)
    assert p.src.w == pytest.approx(540 * 1080 / 672)  # horizontally cropped
    # Visible part is centred inside the crop.
    assert p.src.x == pytest.approx((960 - p.src.w) / 2)
    # Pixels stay square: same scale on both axes.
    assert p.dst.w / p.src.w == pytest.approx(p.dst.h / p.src.h)


def test_fit_shows_whole_crop_with_bars():
    comp = Composition().with_region(WEBCAM, Region(Rect(0, 0, 0.5, 0.5), fit=FitMode.FIT))
    p = placements(comp)[WEBCAM]
    approx_rect(p.src, 0, 0, 960, 540)  # nothing cut
    assert p.dst.w == pytest.approx(1080)
    assert p.dst.h == pytest.approx(607.5)
    assert p.dst.y == pytest.approx((672 - 607.5) / 2)  # centred vertically


def test_offset_moves_visible_window():
    base = Region(Rect(0, 0, 0.5, 0.5))
    comp = Composition().with_region(WEBCAM, replace(base, offset_x=1.0))
    right = placements(comp)[WEBCAM]
    assert right.src.x == pytest.approx(0)  # content pushed right → left part visible
    comp = Composition().with_region(WEBCAM, replace(base, offset_x=-1.0))
    left = placements(comp)[WEBCAM]
    assert left.src.right == pytest.approx(960)
    # Offset in FIT mode moves the picture inside the slot instead.
    comp = Composition().with_region(WEBCAM, replace(base, fit=FitMode.FIT, offset_y=-1.0))
    assert placements(comp)[WEBCAM].dst.y == pytest.approx(0)


def test_scale_zooms_in():
    comp = Composition().with_region(WEBCAM, Region(Rect(0, 0, 0.5, 0.5), scale=2.0))
    p = placements(comp)[WEBCAM]
    approx_rect(p.dst, 0, 0, 1080, 672)
    assert p.src.h == pytest.approx(270)
    # Zooming out below the slot leaves bars but never leaves the slot.
    comp = Composition().with_region(WEBCAM, Region(Rect(0, 0, 0.5, 0.5), scale=0.5))
    p = placements(comp)[WEBCAM]
    assert p.dst.w < 1080 and p.dst.h < 672
    slot = comp.slots()[WEBCAM]
    assert p.dst.intersected(slot) == p.dst


def test_placements_never_leave_their_slot_or_the_source():
    for fit in FitMode:
        for scale in (0.25, 0.8, 1.0, 3.0):
            for off in (-1.0, -0.3, 0.0, 1.0):
                region = Region(Rect(0.1, 0.2, 0.37, 0.61), fit=fit, scale=scale, offset_x=off, offset_y=-off)
                comp = Composition(divider=0.42).with_region(GAMEPLAY, region)
                p = placements(comp)[GAMEPLAY]
                assert p.dst.intersected(p.slot) == p.dst
                crop = comp.crop_px(GAMEPLAY, *SRC)
                eps = 1e-6
                assert p.src.x >= crop.x - eps and p.src.right <= crop.right + eps
                assert p.src.y >= crop.y - eps and p.src.bottom <= crop.bottom + eps
                assert p.dst.w / p.src.w == pytest.approx(p.dst.h / p.src.h)


def test_region_values_are_clamped():
    region = Region(Rect(0.9, -0.5, 0.5, 0.001), scale=100, offset_x=7).cleaned()
    assert region.crop.right <= 1.0 and region.crop.x >= 0 and region.crop.y >= 0
    assert region.crop.h == pytest.approx(0.02)
    assert region.scale == 8.0 and region.offset_x == 1.0


def test_match_slot_aspect():
    comp = match_slot_aspect(Composition(), GAMEPLAY, *SRC)
    crop = comp.crop_px(GAMEPLAY, *SRC)
    slot = comp.slots()[GAMEPLAY]
    assert crop.w / crop.h == pytest.approx(slot.w / slot.h)
    assert crop.x >= 0 and crop.right <= 1920 + 1e-6 and crop.bottom <= 1080 + 1e-6
    # With a matching aspect, FILL shows the entire crop.
    p = placements(comp)[GAMEPLAY]
    approx_rect(p.src, crop.x, crop.y, crop.w, crop.h)


def test_default_composition_uses_full_slots():
    for corner in ("top-left", "bottom-right"):
        comp = default_composition(1920, 1080, corner)
        for p in comp.layout(1920, 1080):
            approx_rect(p.dst, p.slot.x, p.slot.y, p.slot.w, p.slot.h)
    # Works for other source shapes too (e.g. 4:3).
    assert len(default_composition(1440, 1080).layout(1440, 1080)) == 2


def test_composition_roundtrip_and_validation():
    comp = Composition(divider=0.4, swapped=True).with_region(
        WEBCAM, Region(Rect(0.7, 0.6, 0.25, 0.3), fit=FitMode.FIT, scale=1.5, offset_x=-0.5)
    )
    assert Composition.from_dict(comp.to_dict()) == comp
    for bad in (None, [], {"webcam": {}}, {**comp.to_dict(), "output": [0, 0]},
                {**comp.to_dict(), "webcam": {"crop": [0, 0, 1]}},
                {**comp.to_dict(), "gameplay": {"crop": [0, 0, 1, 1], "fit": "stretch"}}):
        with pytest.raises((ValueError, TypeError)):
            Composition.from_dict(bad)


def test_project_file_stores_composition(tmp_path):
    comp = default_composition(1920, 1080, "bottom-left").with_divider(0.3)
    path = save_project(Project(name="p", composition=comp), tmp_path / "p.tssproj")
    assert load_project(path).composition == comp
    # Old Phase 1 files without a composition still open.
    (tmp_path / "old.tssproj").write_text('{"schema_version": 1, "media": []}', encoding="utf-8")
    assert load_project(tmp_path / "old.tssproj").composition is None
    (tmp_path / "bad.tssproj").write_text('{"schema_version": 1, "composition": {"webcam": 1}}', encoding="utf-8")
    with pytest.raises(ProjectFormatError):
        load_project(tmp_path / "bad.tssproj")


def test_preset_store(tmp_path):
    store = PresetStore(tmp_path / "presets.json")
    builtin = [p.name for p in store.all()]
    assert "Webcam top left" in builtin and "Gameplay only" in builtin

    custom = Composition(divider=0.25)
    store.save("  My layout ", custom)
    with pytest.raises(ValueError):
        store.save("Gameplay only", custom)
    with pytest.raises(ValueError):
        store.save("   ", custom)

    reopened = PresetStore(tmp_path / "presets.json")
    assert reopened.get("My layout").composition == custom
    assert not reopened.delete("Gameplay only")
    assert reopened.delete("My layout")
    assert PresetStore(tmp_path / "presets.json").get("My layout") is None

    (tmp_path / "presets.json").write_text("{broken", encoding="utf-8")
    assert len(PresetStore(tmp_path / "presets.json").all()) == len(builtin)

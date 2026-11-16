"""Mouse-driven composition editing (offscreen)."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.editor.composition import GAMEPLAY, WEBCAM, FitMode
from core.editor.geometry import Rect
from core.editor.presets import PresetStore
from ui.editor.composition_controller import CompositionController
from ui.panels.composition_panel import CompositionPanel
from ui.preview.output_view import OutputView
from ui.preview.source_view import SourceView, resize_rect


def frame(w=1920, h=1080) -> QImage:
    image = QImage(w, h, QImage.Format.Format_RGB32)
    image.fill(QColor("#204060"))
    return image


def drag(widget, start: QPointF, end: QPointF, modifiers=Qt.KeyboardModifier.NoModifier):
    a, b = start.toPoint(), end.toPoint()
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, modifiers, a)
    mid = QPoint((a.x() + b.x()) // 2, (a.y() + b.y()) // 2)
    QTest.mouseMove(widget, mid)
    QTest.mouseMove(widget, b)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, modifiers, b)
    QApplication.processEvents()


@pytest.fixture
def controller(qapp):
    c = CompositionController()
    c.set_crop_px(WEBCAM, Rect(0, 0, 480, 270))
    c.set_crop_px(GAMEPLAY, Rect(600, 100, 800, 800))
    return c


@pytest.fixture
def source_view(controller):
    view = SourceView(controller)
    view.resize(960, 540)  # exactly half scale: 1 widget px = 2 source px
    view.set_image(frame())
    view.show()
    QApplication.processEvents()
    return view


@pytest.fixture
def output_view(controller):
    view = OutputView(controller)
    view.resize(290, 500)  # canvas 270x480 with 10 px margin → 0.25 scale
    view.set_image(frame())
    view.show()
    QApplication.processEvents()
    return view


def test_resize_rect_respects_bounds_and_min_size():
    bounds = Rect(0, 0, 1920, 1080)
    start = Rect(100, 100, 400, 300)
    assert resize_rect(start, "br", 50, 20, bounds, 16) == Rect(100, 100, 450, 320)
    assert resize_rect(start, "tl", -500, -500, bounds, 16) == Rect(0, 0, 500, 400)
    shrunk = resize_rect(start, "r", -1000, 0, bounds, 16)
    assert shrunk.w == 16 and shrunk.x == 100
    assert resize_rect(start, "b", 0, 5000, bounds, 16).bottom == 1080


def test_drag_moves_crop(source_view, controller):
    # Webcam crop occupies widget (0,0)-(240,135). Drag its centre by (+100, +50) widget px.
    drag(source_view, QPointF(120, 70), QPointF(220, 120))
    crop = controller.crop_px(WEBCAM)
    assert (round(crop.x), round(crop.y)) == (200, 100)
    assert (round(crop.w), round(crop.h)) == (480, 270)


def test_move_is_clamped_to_frame(source_view, controller):
    drag(source_view, QPointF(120, 70), QPointF(-400, -400))
    crop = controller.crop_px(WEBCAM)
    assert (round(crop.x), round(crop.y)) == (0, 0)


def test_drag_handle_resizes_crop(source_view, controller):
    # Bottom-right handle of the selected webcam crop is at widget (240, 135).
    drag(source_view, QPointF(240, 135), QPointF(300, 185))
    crop = controller.crop_px(WEBCAM)
    assert (round(crop.w), round(crop.h)) == (600, 370)
    assert (round(crop.x), round(crop.y)) == (0, 0)


def test_click_selects_other_region_and_moves_it(source_view, controller):
    assert controller.selected == WEBCAM
    # Gameplay crop is widget (300,50)-(700,450).
    drag(source_view, QPointF(500, 250), QPointF(520, 250))
    assert controller.selected == GAMEPLAY
    assert round(controller.crop_px(GAMEPLAY).x) == 640


def test_shift_drag_draws_new_crop(source_view, controller):
    drag(source_view, QPointF(100, 100), QPointF(400, 300), Qt.KeyboardModifier.ShiftModifier)
    crop = controller.crop_px(WEBCAM)
    assert (round(crop.x), round(crop.y), round(crop.w), round(crop.h)) == (200, 200, 600, 400)


def test_disabled_region_is_not_hit(source_view, controller):
    controller.update_region(GAMEPLAY, enabled=False)
    name, mode = source_view.hit_test(QPointF(500, 250))
    assert (name, mode) == (WEBCAM, "draw")


def test_output_view_divider_drag(output_view, controller):
    y = output_view._divider_y()
    assert y == pytest.approx(10 + 672 * 0.25)
    drag(output_view, QPointF(145, y), QPointF(145, 10 + 240))
    assert controller.composition.divider == pytest.approx(0.5, abs=0.01)


def test_output_view_pan_and_zoom(output_view, controller):
    # Webcam 480x270 in fill mode overflows horizontally in the top slot.
    drag(output_view, QPointF(145, 60), QPointF(245, 60))
    assert controller.selected == WEBCAM
    assert controller.region(WEBCAM).offset_x > 0
    assert controller.region(WEBCAM).offset_y == 0  # no free space vertically

    controller.update_region(GAMEPLAY, scale=1.0)
    assert output_view.region_at(QPointF(145, 400)) == GAMEPLAY
    QTest.mouseDClick(output_view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(145, 60))
    assert controller.region(WEBCAM).offset_x == 0


def test_output_view_renders_both_regions(output_view, controller):
    image = output_view.grab().toImage()
    # Centre of each slot shows the frame colour, not the black background.
    for y in (60, 350):
        assert image.pixelColor(145, y).blue() > 60


def test_panel_edits_update_model_and_back(controller, tmp_path):
    panel = CompositionPanel(controller, PresetStore(tmp_path / "presets.json"))
    assert panel.crop_w.value() == 480
    panel.crop_x.setValue(100)
    assert round(controller.crop_px(WEBCAM).x) == 100

    panel.gameplay_button.click()
    assert controller.selected == GAMEPLAY
    assert panel.crop_w.value() == 800
    panel.fit_combo.setCurrentIndex(panel.fit_combo.findData(FitMode.FIT.value))
    assert controller.region(GAMEPLAY).fit is FitMode.FIT
    panel.scale_spin.setValue(150)
    assert controller.region(GAMEPLAY).scale == pytest.approx(1.5)

    panel.divider_spin.setValue(45)
    assert controller.composition.divider == pytest.approx(0.45)
    panel.swap_button.click()
    assert controller.composition.swapped

    panel.enabled_check.setChecked(False)
    assert not controller.region(GAMEPLAY).enabled
    assert not panel.divider_spin.isEnabled() and not panel.crop_x.isEnabled()

    # Model changes flow back into the widgets.
    controller.update_region(GAMEPLAY, enabled=True)
    controller.set_crop_px(GAMEPLAY, Rect(10, 20, 300, 200))
    assert (panel.crop_x.value(), panel.crop_y.value()) == (10, 20)


def test_panel_presets(controller, tmp_path):
    store = PresetStore(tmp_path / "presets.json")
    panel = CompositionPanel(controller, store)
    controller.set_divider(0.6)
    assert panel.save_preset("Tall cam")
    assert panel.preset_combo.currentText() == "Tall cam"
    assert panel.delete_preset_button.isEnabled()

    index = panel.preset_combo.findText("Gameplay only")
    panel.preset_combo.setCurrentIndex(index)
    panel.preset_combo.activated.emit(index)
    assert not controller.region(WEBCAM).enabled
    assert not panel.delete_preset_button.isEnabled()

    index = panel.preset_combo.findText("Tall cam")
    panel.preset_combo.setCurrentIndex(index)
    panel.preset_combo.activated.emit(index)
    assert controller.composition.divider == pytest.approx(0.6)


def test_composition_saved_with_project(qapp, data_dir, ffmpeg_paths, sample_clips, tmp_path):
    from app.config import AppConfig
    from tests.test_ui import _wait
    from ui.main_window.main_window import MainWindow

    window = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    window.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: window.media_panel.list.count() == 1)
    assert window.composition.source_size == (1280, 720)
    assert window.project.composition is not None

    window._set_dirty(False)
    window.composition.set_crop_px(WEBCAM, Rect(900, 500, 300, 200))
    window.composition.set_divider(0.3)
    assert window._dirty

    window.project_path = tmp_path / "comp.tssproj"
    assert window.save()
    saved = window.composition.composition
    window.close()

    other = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    other.open_project(tmp_path / "comp.tssproj")
    assert other.composition.composition == saved
    assert not other._dirty
    assert _wait(lambda: other.media_panel.list.count() == 1)
    assert other.composition.composition == saved  # default must not overwrite it
    assert not other._dirty
    other.close()

import copy
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6 import QtCore, QtGui, QtWidgets

    from anylabeling.config import update_dict
    from anylabeling.views.labeling.settings.schema import (
        load_template_config,
    )
    from anylabeling.views.labeling.shape import Shape
    from anylabeling.views.labeling.widgets.canvas import Canvas

    PYQT_AVAILABLE = True
except Exception:
    PYQT_AVAILABLE = False


def _near(color, red, green, blue, tolerance=28):
    return (
        abs(color.red() - red) <= tolerance
        and abs(color.green() - green) <= tolerance
        and abs(color.blue() - blue) <= tolerance
    )


@unittest.skipUnless(
    PYQT_AVAILABLE, "PyQt6 is required for selected-fill tests"
)
class TestSelectedShapeFill(unittest.TestCase):

    def setUp(self):
        self.app = QtWidgets.QApplication.instance()
        if self.app is None:
            self.app = QtWidgets.QApplication([])
        self.canvas = Canvas(parent=None, mask={"opacity": 255})
        self.canvas.resize(240, 240)
        self.canvas.pixmap = QtGui.QPixmap(240, 240)
        self.canvas.pixmap.fill(QtGui.QColor(200, 200, 200))
        self.canvas.show_texts = False
        self.canvas.show_labels = False
        self.canvas.show_scores = False
        self.canvas.show_attributes = False
        self.canvas.show_groups = False
        self.canvas.show_degrees = False
        self.canvas.show_linking = False
        self.canvas.cross_line_show = False
        self.canvas.show_masks = True
        self.canvas.set_fill_drawing(True)
        self.canvas.fill_selected = False
        self.canvas.scale = 1.0

    def tearDown(self):
        self.canvas.close()
        self.app.processEvents()

    def _polygon(self, selected=False):
        shape = Shape(label="contour", shape_type="polygon")
        shape.points = [
            QtCore.QPointF(50, 50),
            QtCore.QPointF(190, 50),
            QtCore.QPointF(190, 190),
            QtCore.QPointF(50, 190),
        ]
        shape.close()
        shape.line_color = QtGui.QColor(255, 0, 0)
        shape.select_line_color = QtGui.QColor(0, 0, 255)
        shape.line_width = 3
        shape.point_size = 14
        shape.selected = selected
        return shape

    def _render(self):
        self.canvas.show()
        self.app.processEvents()
        image = self.canvas.grab().toImage()
        offset = self.canvas.offset_to_center()
        return image, int(offset.x()), int(offset.y())

    def _at(self, image, x, y, origin_x, origin_y):
        return image.pixelColor(x + origin_x, y + origin_y)

    def _has_control_point(self, image, origin_x, origin_y):
        for y in range(36, 66):
            for x in range(36, 66):
                color = image.pixelColor(x + origin_x, y + origin_y)
                if (
                    color.green() > 150
                    and color.green() > color.blue() + 40
                    and color.green() > color.red() + 40
                ):
                    return True
        return False

    def test_selected_polygon_can_hide_interior_fill(self):
        shape = self._polygon(selected=True)
        points = [(point.x(), point.y()) for point in shape.points]
        self.canvas.shapes = [shape]
        self.canvas.selected_shapes = [shape]
        self.canvas.fill_selected = False
        self.canvas.mask_opacity = 255

        image, origin_x, origin_y = self._render()
        center = self._at(image, 120, 120, origin_x, origin_y)
        edge = self._at(image, 120, 50, origin_x, origin_y)
        self.assertTrue(
            _near(center, 200, 200, 200),
            f"selected interior stayed covered: {center.getRgb()}",
        )
        self.assertGreater(edge.blue(), 120)
        self.assertTrue(self._has_control_point(image, origin_x, origin_y))
        self.assertEqual(
            [(point.x(), point.y()) for point in shape.points], points
        )
        self.assertEqual(shape.label, "contour")

        self.canvas.fill_selected = True
        image, origin_x, origin_y = self._render()
        covered = self._at(image, 120, 120, origin_x, origin_y)
        self.assertTrue(
            _near(covered, 0, 0, 255),
            f"selected fill did not use the outline color: {covered.getRgb()}",
        )
        self.assertGreater(
            self._at(image, 120, 50, origin_x, origin_y).blue(), 120
        )
        self.assertTrue(self._has_control_point(image, origin_x, origin_y))

    def test_mask_opacity_changes_selected_fill(self):
        shape = self._polygon(selected=True)
        self.canvas.shapes = [shape]
        self.canvas.fill_selected = True
        self.canvas.mask_opacity = 0
        image, origin_x, origin_y = self._render()
        clear = self._at(image, 120, 120, origin_x, origin_y)
        self.assertTrue(_near(clear, 200, 200, 200), clear.getRgb())

        self.canvas.mask_opacity = 255
        image, origin_x, origin_y = self._render()
        opaque = self._at(image, 120, 120, origin_x, origin_y)
        self.assertTrue(_near(opaque, 0, 0, 255), opaque.getRgb())
        self.assertFalse(
            _near(opaque, clear.red(), clear.green(), clear.blue())
        )

    def test_unselected_mask_and_drawing_fill_stay(self):
        shape = self._polygon(selected=False)
        self.canvas.shapes = [shape]
        self.canvas.fill_selected = False
        self.canvas.mask_opacity = 255
        image, origin_x, origin_y = self._render()
        resting = self._at(image, 120, 120, origin_x, origin_y)
        self.assertTrue(
            _near(resting, 255, 0, 0),
            f"unselected mask changed: {resting.getRgb()}",
        )

        self.canvas.shapes = []
        current = Shape(shape_type="polygon")
        current.points = [
            QtCore.QPointF(50, 50),
            QtCore.QPointF(190, 50),
            QtCore.QPointF(190, 190),
        ]
        current.fill_color = QtGui.QColor(0, 180, 0, 255)
        current.line_color = QtGui.QColor(0, 180, 0, 255)
        current.selected = False
        self.canvas.current = current
        self.canvas.create_mode = "polygon"
        self.canvas.line = Shape()
        self.canvas.line.points = [
            QtCore.QPointF(190, 190),
            QtCore.QPointF(50, 190),
        ]
        image, origin_x, origin_y = self._render()
        drawing = self._at(image, 120, 120, origin_x, origin_y)
        self.assertGreater(drawing.green(), 100)
        self.assertLess(drawing.red(), 80)

    def test_brush_shape_is_not_filled_by_the_selected_switch(self):
        shape = self._polygon(selected=True)
        shape._brush_using_mask = True
        self.canvas.shapes = [shape]
        self.canvas.fill_selected = True
        self.canvas.mask_opacity = 255
        image, origin_x, origin_y = self._render()
        center = self._at(image, 120, 120, origin_x, origin_y)
        self.assertTrue(_near(center, 200, 200, 200), center.getRgb())

    def test_missing_user_value_keeps_the_default_off(self):
        config = copy.deepcopy(load_template_config())
        self.assertFalse(config["canvas"]["mask"]["fill_selected"])
        update_dict(config, {"canvas": {"mask": {"opacity": 40}}})
        self.assertFalse(config["canvas"]["mask"]["fill_selected"])
        self.assertEqual(config["canvas"]["mask"]["opacity"], 40)
        restarted = copy.deepcopy(load_template_config())
        update_dict(restarted, {"canvas": {"mask": {"fill_selected": True}}})
        self.assertTrue(restarted["canvas"]["mask"]["fill_selected"])
        self.assertEqual(restarted["canvas"]["mask"]["opacity"], 80)

import os
import unittest
import warnings

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6 import QtCore, QtGui

    from anylabeling.views.labeling import utils
    from anylabeling.views.labeling.shape import Shape, _screen_pen_width

    PYQT_AVAILABLE = True
except Exception:
    PYQT_AVAILABLE = False


@unittest.skipUnless(PYQT_AVAILABLE, "PyQt6 is required for Qt utility tests")
class TestQtUtils(unittest.TestCase):

    def test_shape_pen_width_stays_two_screen_pixels_at_any_zoom(self):
        original_scale = Shape.scale
        original_width = Shape.line_width
        try:
            Shape.line_width = 2
            shape = Shape(label="line", shape_type="line")
            shape.points = [
                QtCore.QPointF(1.0, 4.0),
                QtCore.QPointF(12.0, 4.0),
            ]
            for scale in (0.25, 1.0, 1.5, 3.0, 8.0):
                Shape.scale = scale
                logical_width = _screen_pen_width(2, scale)
                self.assertAlmostEqual(logical_width * scale, 2.0)

                image = QtGui.QImage(
                    128,
                    64,
                    QtGui.QImage.Format.Format_ARGB32,
                )
                painter = QtGui.QPainter(image)
                painter.scale(scale, scale)
                shape.paint(painter)
                screen_width = (
                    painter.worldTransform().m11()
                    * painter.pen().widthF()
                )
                painter.end()
                self.assertAlmostEqual(screen_width, 2.0)
        finally:
            Shape.scale = original_scale
            Shape.line_width = original_width


    def test_distance_to_line_handles_2d_points_without_numpy_warning(self):
        point = QtCore.QPointF(5.0, 5.0)
        line = [QtCore.QPointF(0.0, 0.0), QtCore.QPointF(10.0, 0.0)]

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            distance = utils.distance_to_line(point, line)

        self.assertEqual(distance, 5.0)

    def test_nearest_edge_handles_line_shapes_without_numpy_warning(self):
        shape = Shape(label="line", shape_type="line")
        shape.points = [
            QtCore.QPointF(0.0, 0.0),
            QtCore.QPointF(10.0, 0.0),
        ]

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            edge = shape.nearest_edge(QtCore.QPointF(5.0, 1.0), 2.0)

        self.assertIn(edge, (0, 1))

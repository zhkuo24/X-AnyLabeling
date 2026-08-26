import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6 import QtCore, QtGui, QtWidgets

    from anylabeling.views.labeling.shape import Shape
    from anylabeling.views.labeling.widgets.canvas import Canvas

    PYQT_AVAILABLE = True
except Exception:
    PYQT_AVAILABLE = False


@unittest.skipUnless(
    PYQT_AVAILABLE, "PyQt6 is required for Pencil drawing tests"
)
class TestCanvasPencilDrawing(unittest.TestCase):

    def setUp(self):
        self.app = QtWidgets.QApplication.instance()
        if self.app is None:
            self.app = QtWidgets.QApplication([])
        self.canvas = Canvas(
            parent=None,
            pencil={
                "point_distance": 10.0,
                "smoothing": 0.65,
                "simplify_epsilon": 0.0,
                "close_distance": 15.0,
            },
        )
        self.canvas.resize(100, 100)
        self.canvas.pixmap = QtGui.QPixmap(100, 100)
        self.canvas.scale = 1.0
        self.canvas.set_editing(False)
        self.canvas.create_mode = "polygon"
        self.canvas.set_pencil_mode(True)

    def tearDown(self):
        self.canvas.close()
        self.app.processEvents()

    def mouse_event(self, event_type, pos, button, buttons):
        event = QtGui.QMouseEvent(
            event_type,
            QtCore.QPointF(*pos),
            QtCore.QPointF(*pos),
            button,
            buttons,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        if event_type == QtCore.QEvent.Type.MouseButtonPress:
            self.canvas.mousePressEvent(event)
        elif event_type == QtCore.QEvent.Type.MouseMove:
            self.canvas.mouseMoveEvent(event)
        else:
            self.canvas.mouseReleaseEvent(event)
        return event

    def press(self, pos):
        return self.mouse_event(
            QtCore.QEvent.Type.MouseButtonPress,
            pos,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.LeftButton,
        )

    def move(self, pos):
        return self.mouse_event(
            QtCore.QEvent.Type.MouseMove,
            pos,
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.MouseButton.LeftButton,
        )

    def release(self, pos):
        return self.mouse_event(
            QtCore.QEvent.Type.MouseButtonRelease,
            pos,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.NoButton,
        )

    class TabletEventStub:
        def __init__(self, event_type, pos):
            self._event_type = event_type
            self._pos = QtCore.QPointF(*pos)
            self.accepted = False

        def type(self):
            return self._event_type

        def position(self):
            return self._pos

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.accepted = False

    def test_press_move_release_closes_polygon(self):
        self.press((10.0, 10.0))
        for point in (
            (60.0, 10.0),
            (60.0, 60.0),
            (10.0, 60.0),
        ):
            self.move(point)
        self.release((11.0, 11.0))

        self.assertIsNone(self.canvas.current)
        self.assertFalse(self.canvas._pencil_stroke_active)
        self.assertEqual(len(self.canvas.shapes), 1)
        self.assertTrue(self.canvas.shapes[0].is_closed())

    def test_fast_move_is_interpolated_at_configured_spacing(self):
        self.press((10.0, 10.0))
        self.move((50.0, 10.0))

        xs = [round(point.x()) for point in self.canvas.current.points]
        self.assertEqual(xs, [10, 20, 30, 40, 50])

    def test_adaptive_smoothing_damps_slow_jitter_but_tracks_fast_moves(self):
        self.canvas.pencil_smoothing = 1.0
        self.canvas._pencil_filtered_pos = QtCore.QPointF(10.0, 10.0)

        slow_raw = QtCore.QPointF(12.0, 11.0)
        slow_filtered = self.canvas._filter_pencil_pos(slow_raw)
        self.assertLess(
            (slow_filtered - QtCore.QPointF(10.0, 10.0)).manhattanLength(),
            (slow_raw - QtCore.QPointF(10.0, 10.0)).manhattanLength(),
        )

        fast_raw = QtCore.QPointF(80.0, 80.0)
        fast_filtered = self.canvas._filter_pencil_pos(fast_raw)
        self.assertAlmostEqual(fast_filtered.x(), fast_raw.x())
        self.assertAlmostEqual(fast_filtered.y(), fast_raw.y())

    def test_close_target_snaps_preview_and_tracks_candidate_state(self):
        self.press((10.0, 10.0))
        self.move((60.0, 10.0))
        self.move((60.0, 60.0))
        self.move((12.0, 12.0))

        self.assertTrue(self.canvas._pencil_close_candidate)
        self.assertEqual(self.canvas.line[1], self.canvas.current[0])

        self.move((60.0, 60.0))

        self.assertFalse(self.canvas._pencil_close_candidate)
        self.assertNotEqual(self.canvas.line[1], self.canvas.current[0])

    def test_close_target_feedback_paints_on_canvas(self):
        self.canvas.current = Shape(shape_type="polygon")
        self.canvas.current.points = [
            QtCore.QPointF(40.0, 40.0),
            QtCore.QPointF(70.0, 40.0),
            QtCore.QPointF(70.0, 70.0),
        ]
        image = QtGui.QImage(
            100,
            100,
            QtGui.QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(image)
        try:
            self.canvas._paint_pencil_closure_feedback(painter)
        finally:
            painter.end()

        painted_pixels = sum(
            image.pixelColor(x, y).alpha() > 0
            for x in range(image.width())
            for y in range(image.height())
        )
        self.assertGreater(painted_pixels, 0)

    def test_move_without_press_does_not_start_contour(self):
        self.move((50.0, 50.0))

        self.assertIsNone(self.canvas.current)
        self.assertEqual(self.canvas.shapes, [])

    def test_press_outside_image_does_not_start_or_resume_contour(self):
        self.press((120.0, 50.0))
        self.assertIsNone(self.canvas.current)

        self.press((10.0, 10.0))
        self.move((60.0, 10.0))
        self.release((60.0, 60.0))
        point_count = len(self.canvas.current.points)

        self.press((120.0, 50.0))

        self.assertFalse(self.canvas._pencil_stroke_active)
        self.assertEqual(len(self.canvas.current.points), point_count)

    def test_native_tablet_events_use_the_same_stroke_pipeline(self):
        events = [
            self.TabletEventStub(
                QtCore.QEvent.Type.TabletPress, (10.0, 10.0)
            ),
            self.TabletEventStub(
                QtCore.QEvent.Type.TabletMove, (60.0, 10.0)
            ),
            self.TabletEventStub(
                QtCore.QEvent.Type.TabletMove, (10.0, 60.0)
            ),
            self.TabletEventStub(
                QtCore.QEvent.Type.TabletRelease, (10.0, 10.0)
            ),
        ]

        for event in events:
            self.canvas.tabletEvent(event)

        self.assertTrue(all(event.accepted for event in events))
        self.assertIsNone(self.canvas.current)
        self.assertEqual(len(self.canvas.shapes), 1)

    def test_far_release_keeps_contour_open_for_continuation(self):
        incomplete = []
        self.canvas.pencil_stroke_incomplete.connect(
            lambda: incomplete.append(True)
        )
        self.press((10.0, 10.0))
        self.move((60.0, 10.0))
        self.release((60.0, 60.0))

        self.assertIsNotNone(self.canvas.current)
        self.assertFalse(self.canvas.current.is_closed())
        self.assertFalse(self.canvas._pencil_stroke_active)
        self.assertEqual(incomplete, [True])

        self.press((60.0, 60.0))
        self.move((10.0, 60.0))
        self.release((10.0, 10.0))

        self.assertIsNone(self.canvas.current)
        self.assertEqual(len(self.canvas.shapes), 1)

    def test_too_short_stroke_is_cancelled(self):
        self.press((10.0, 10.0))
        self.release((12.0, 12.0))

        self.assertIsNone(self.canvas.current)
        self.assertEqual(self.canvas.shapes, [])

    def test_disabling_pencil_mode_cancels_an_incomplete_contour(self):
        drawing_states = []
        self.canvas.drawing_polygon.connect(drawing_states.append)
        self.press((10.0, 10.0))
        self.move((60.0, 10.0))

        self.canvas.set_pencil_mode(False)

        self.assertFalse(self.canvas._pencil_mode)
        self.assertFalse(self.canvas._pencil_stroke_active)
        self.assertIsNone(self.canvas.current)
        self.assertEqual(drawing_states, [True, False])

    def test_rdp_simplifies_pencil_points_without_spline_fitting(self):
        self.canvas.pencil_simplify_epsilon_px = 0.5
        self.canvas.current = Shape(shape_type="polygon")
        self.canvas.current.points = [
            QtCore.QPointF(10.0, 10.0),
            QtCore.QPointF(20.0, 10.1),
            QtCore.QPointF(30.0, 9.9),
            QtCore.QPointF(40.0, 10.0),
            QtCore.QPointF(40.0, 40.0),
            QtCore.QPointF(10.0, 40.0),
        ]

        self.canvas._simplify_pencil_contour()

        self.assertLess(len(self.canvas.current.points), 6)
        self.assertGreaterEqual(len(self.canvas.current.points), 3)

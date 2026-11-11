import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6 import QtCore, QtGui, QtTest, QtWidgets

    from anylabeling.views.labeling.shape import Shape
    from anylabeling.views.labeling.widgets.canvas import Canvas

    PYQT_AVAILABLE = True
except Exception:
    PYQT_AVAILABLE = False

ARTIFACTS = Path("/opt/cursor/artifacts")


def _near(color, red, green, blue, tolerance=30):
    return (
        abs(int(color.red()) - red) <= tolerance
        and abs(int(color.green()) - green) <= tolerance
        and abs(int(color.blue()) - blue) <= tolerance
    )


def _is_blue(color):
    return (
        color.blue() > 140
        and color.blue() > color.red() + 40
        and color.blue() > color.green() + 40
    )


def _is_background(color):
    return _near(color, 200, 200, 200, tolerance=18)


def _key(event_type, key, modifiers, autorepeat=False):
    return QtGui.QKeyEvent(
        event_type,
        key,
        modifiers,
        "",
        autorepeat,
        1,
    )


def _mouse(event_type, pos, button, buttons):
    point = QtCore.QPointF(pos)
    return QtGui.QMouseEvent(
        event_type,
        point,
        point,
        button,
        buttons,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )


@unittest.skipUnless(
    PYQT_AVAILABLE, "PyQt6 is required for edit-outline tests"
)
class TestEditOutline(unittest.TestCase):
    def setUp(self):
        self.app = QtWidgets.QApplication.instance()
        if self.app is None:
            self.app = QtWidgets.QApplication([])
        self.canvases = []

    def tearDown(self):
        for canvas in self.canvases:
            canvas.close()
        self.app.processEvents()

    def _canvas(self, width=240, height=240, scale=1.0, mask=None):
        canvas = Canvas(
            parent=None,
            mask={"opacity": 80} if mask is None else mask,
        )
        canvas.resize(width, height)
        canvas.pixmap = QtGui.QPixmap(int(width / scale), int(height / scale))
        canvas.pixmap.fill(QtGui.QColor(200, 200, 200))
        canvas.show_texts = False
        canvas.show_labels = False
        canvas.show_scores = False
        canvas.show_attributes = False
        canvas.show_groups = False
        canvas.show_degrees = False
        canvas.show_linking = False
        canvas.cross_line_show = False
        canvas.show_masks = True
        canvas.fill_selected = False
        canvas.scale = scale
        canvas.mode = Canvas.EDIT
        self.canvases.append(canvas)
        return canvas

    def _polygon(self, selected=True, line_width=8):
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
        shape.line_width = line_width
        shape.point_size = 14
        shape.selected = selected
        shape.visible = True
        return shape

    def _render(self, canvas):
        canvas.show()
        self.app.processEvents()
        image = canvas.grab().toImage()
        offset = canvas.offset_to_center()
        return image, int(round(offset.x())), int(round(offset.y()))

    def _color(self, image, x, y, origin_x, origin_y):
        return image.pixelColor(
            int(x) + origin_x,
            int(y) + origin_y,
        )

    def _scan_edge(self, image, origin_x, origin_y, y0, y1, x0, x1):
        best = None
        ink_rows = 0
        for y in range(y0, y1 + 1):
            line = 0
            gap = 0
            for x in range(x0, x1 + 1):
                color = self._color(image, x, y, origin_x, origin_y)
                if _is_blue(color):
                    line += 1
                elif _is_background(color):
                    gap += 1
            if line > 2:
                ink_rows += 1
            if best is None or line > best[0]:
                best = (line, gap, y)
        return best, ink_rows

    def _snapshot(self, shape):
        return {
            "points": [(point.x(), point.y()) for point in shape.points],
            "label": shape.label,
            "visible": shape.visible,
            "selected": shape.selected,
        }

    def _save(self, image, name):
        if ARTIFACTS.is_dir():
            image.save(str(ARTIFACTS / name))

    def test_missing_key_defaults_dashed_and_saved_solid_is_kept(self):
        dashed = self._canvas(mask={"opacity": 40})
        solid = self._canvas(
            mask={"opacity": 40, "edit_outline_dashed": False}
        )
        explicit = self._canvas(
            mask={"opacity": 40, "edit_outline_dashed": True}
        )
        self.assertTrue(dashed.edit_outline_dashed)
        self.assertFalse(solid.edit_outline_dashed)
        self.assertTrue(explicit.edit_outline_dashed)
        self.assertFalse(dashed.fill_selected)

    def test_selected_outline_is_thin_dashed_and_solid_can_be_selected(self):
        canvas = self._canvas()
        shape = self._polygon(selected=True, line_width=8)
        before = self._snapshot(shape)
        canvas.shapes = [shape]
        canvas.selected_shapes = [shape]
        canvas.edit_outline_dashed = True

        dashed, ox, oy = self._render(canvas)
        dashed_best, dashed_rows = self._scan_edge(
            dashed, ox, oy, 42, 62, 80, 160
        )
        self._save(dashed, "selected-outline-dashed.png")
        self.assertGreaterEqual(dashed_best[0], 4, dashed_best)
        self.assertGreaterEqual(dashed_best[1], 3, dashed_best)
        self.assertLessEqual(dashed_rows, 4, dashed_rows)
        handle = self._color(dashed, 50, 50, ox, oy)
        self.assertGreater(handle.green(), 150, handle.getRgb())
        self.assertEqual(self._snapshot(shape), before)

        canvas.edit_outline_dashed = False
        solid, ox, oy = self._render(canvas)
        solid_best, solid_rows = self._scan_edge(
            solid, ox, oy, 42, 62, 80, 160
        )
        self._save(solid, "selected-outline-solid.png")
        self.assertGreaterEqual(solid_best[0], 40, solid_best)
        self.assertLessEqual(solid_best[1], 2, solid_best)
        self.assertLessEqual(solid_rows, 4, solid_rows)
        self.assertEqual(self._snapshot(shape), before)

        compare = QtGui.QImage(
            dashed.width() + solid.width() + 16,
            max(dashed.height(), solid.height()) + 28,
            QtGui.QImage.Format.Format_ARGB32,
        )
        compare.fill(QtGui.QColor(32, 32, 32))
        painter = QtGui.QPainter(compare)
        painter.drawImage(0, 24, dashed)
        painter.drawImage(dashed.width() + 16, 24, solid)
        painter.setPen(QtGui.QColor(255, 255, 255))
        painter.drawText(8, 18, "dashed (default)")
        painter.drawText(dashed.width() + 24, 18, "solid")
        painter.end()
        self._save(compare, "selected-outline-compare.png")

    def test_zoom_keeps_the_selected_outline_thin_and_dashed(self):
        canvas = self._canvas(width=400, height=400, scale=4.0)
        shape = self._polygon(selected=True, line_width=8)
        shape.points = [
            QtCore.QPointF(20, 20),
            QtCore.QPointF(80, 20),
            QtCore.QPointF(80, 80),
            QtCore.QPointF(20, 80),
        ]
        shape.close()
        before = self._snapshot(shape)
        canvas.shapes = [shape]
        canvas.selected_shapes = [shape]
        canvas.edit_outline_dashed = True
        image, ox, oy = self._render(canvas)
        # Painter scale maps image y=20 to screen y=(20+offset)*scale.
        screen_y = int(round((20 + oy) * canvas.scale))
        screen_x0 = int(round((32 + ox) * canvas.scale))
        screen_x1 = int(round((68 + ox) * canvas.scale))
        best, rows = self._scan_edge(
            image,
            0,
            0,
            screen_y - 8,
            screen_y + 8,
            screen_x0,
            screen_x1,
        )
        self.assertGreaterEqual(best[0], 8, best)
        self.assertGreaterEqual(best[1], 6, best)
        self.assertLessEqual(rows, 4, rows)
        self.assertEqual(self._snapshot(shape), before)

    def test_unselected_and_drawing_strokes_keep_the_normal_width(self):
        canvas = self._canvas()
        unselected = self._polygon(selected=False, line_width=8)
        unselected.select_line_color = QtGui.QColor(0, 0, 255)
        unselected.line_color = QtGui.QColor(0, 0, 255)
        canvas.shapes = [unselected]
        image, ox, oy = self._render(canvas)
        _best, rows = self._scan_edge(image, ox, oy, 40, 70, 80, 160)
        self.assertGreaterEqual(rows, 6, rows)

        canvas.mode = Canvas.CREATE
        selected = self._polygon(selected=True, line_width=8)
        selected.points = [
            QtCore.QPointF(50, 120),
            QtCore.QPointF(190, 120),
            QtCore.QPointF(190, 200),
            QtCore.QPointF(50, 200),
        ]
        selected.close()
        current = Shape(shape_type="rectangle")
        current.selected = True
        current.line_width = 8
        current.line_color = QtGui.QColor(0, 0, 255)
        current.select_line_color = QtGui.QColor(0, 0, 255)
        current.points = [
            QtCore.QPointF(50, 40),
            QtCore.QPointF(190, 70),
        ]
        canvas.shapes = [selected]
        canvas.selected_shapes = [selected]
        canvas.current = current
        canvas.edit_outline_dashed = True
        image, ox, oy = self._render(canvas)
        _best, drawing_rows = self._scan_edge(image, ox, oy, 30, 80, 80, 160)
        _selected_best, selected_rows = self._scan_edge(
            image, ox, oy, 112, 132, 80, 160
        )
        self.assertGreaterEqual(drawing_rows, 6, drawing_rows)
        self.assertLessEqual(selected_rows, 4, selected_rows)

        strip = Shape(label="strip", shape_type="linestrip")
        strip.points = [
            QtCore.QPointF(50, 50),
            QtCore.QPointF(190, 50),
        ]
        strip.selected = True
        strip.line_width = 8
        strip.line_color = QtGui.QColor(0, 0, 255)
        strip.select_line_color = QtGui.QColor(0, 0, 255)
        canvas.mode = Canvas.EDIT
        canvas.current = None
        canvas.shapes = [strip]
        canvas.selected_shapes = [strip]
        image, ox, oy = self._render(canvas)
        _best, strip_rows = self._scan_edge(image, ox, oy, 40, 70, 80, 160)
        self.assertGreaterEqual(strip_rows, 6, strip_rows)

    def test_hold_h_hides_completed_shapes_and_restores_them(self):
        canvas = self._canvas()
        shape = self._polygon(selected=True)
        before = self._snapshot(shape)
        canvas.shapes = [shape]
        canvas.selected_shapes = [shape]
        canvas.setFocus()
        shown, ox, oy = self._render(canvas)
        shown_best, _rows = self._scan_edge(shown, ox, oy, 42, 62, 80, 160)
        self.assertGreaterEqual(shown_best[0], 4, shown_best)

        press = _key(
            QtCore.QEvent.Type.KeyPress,
            QtCore.Qt.Key.Key_H,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        canvas.keyPressEvent(press)
        self.assertTrue(canvas._image_peek)
        hidden, ox, oy = self._render(canvas)
        hidden_best, hidden_rows = self._scan_edge(
            hidden, ox, oy, 36, 66, 36, 200
        )
        self.assertEqual(hidden_best[0], 0, hidden_best)
        self.assertEqual(hidden_rows, 0)
        self.assertFalse(self._has_green(hidden, ox, oy, 36, 66, 36, 66))
        self._save(hidden, "selected-outline-peek.png")
        self.assertEqual(self._snapshot(shape), before)
        self.assertFalse(canvas.fill_selected)

        repeat_release = _key(
            QtCore.QEvent.Type.KeyRelease,
            QtCore.Qt.Key.Key_H,
            QtCore.Qt.KeyboardModifier.NoModifier,
            autorepeat=True,
        )
        canvas.keyReleaseEvent(repeat_release)
        self.assertTrue(canvas._image_peek)
        repeat_press = _key(
            QtCore.QEvent.Type.KeyPress,
            QtCore.Qt.Key.Key_H,
            QtCore.Qt.KeyboardModifier.NoModifier,
            autorepeat=True,
        )
        canvas.keyPressEvent(repeat_press)
        self.assertTrue(canvas._image_peek)

        release = _key(
            QtCore.QEvent.Type.KeyRelease,
            QtCore.Qt.Key.Key_H,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        canvas.keyReleaseEvent(release)
        self.assertFalse(canvas._image_peek)
        restored, ox, oy = self._render(canvas)
        restored_best, _rows = self._scan_edge(
            restored, ox, oy, 42, 62, 80, 160
        )
        self.assertGreaterEqual(restored_best[0], 4, restored_best)
        self.assertTrue(self._has_green(restored, ox, oy, 36, 66, 36, 66))
        self.assertEqual(self._snapshot(shape), before)

    def test_peek_clears_without_dropping_the_selection(self):
        canvas = self._canvas()
        shape = self._polygon(selected=True)
        canvas.shapes = [shape]
        canvas.selected_shapes = [shape]
        canvas._set_image_peek(True)
        canvas.focusOutEvent(QtGui.QFocusEvent(QtCore.QEvent.Type.FocusOut))
        self.assertFalse(canvas._image_peek)
        self.assertTrue(shape.selected)
        self.assertEqual(canvas.selected_shapes, [shape])

        canvas._set_image_peek(True)
        canvas.load_pixmap(canvas.pixmap, clear_shapes=False)
        self.assertFalse(canvas._image_peek)
        self.assertTrue(shape.selected)
        self.assertIn(shape, canvas.shapes)

        window = QtWidgets.QWidget()
        window.resize(240, 240)
        hosted = Canvas(parent=window, mask={"opacity": 80})
        hosted.setParent(window)
        hosted.resize(240, 240)
        hosted.pixmap = canvas.pixmap
        hosted.shapes = [shape]
        hosted.selected_shapes = [shape]
        hosted.show_labels = False
        hosted.show_texts = False
        hosted.show_attributes = False
        self.canvases.append(hosted)
        window.show()
        hosted.show()
        self.app.processEvents()
        hosted._set_image_peek(True)
        self.assertIs(hosted._peek_filter_window, window)
        self.app.sendEvent(
            window, QtCore.QEvent(QtCore.QEvent.Type.WindowDeactivate)
        )
        self.assertFalse(hosted._image_peek)
        self.assertTrue(shape.selected)
        window.close()

    def test_hidden_vertex_is_not_dragged_and_drawing_stays_visible(self):
        canvas = self._canvas()
        shape = self._polygon(selected=True, line_width=2)
        canvas.shapes = [shape]
        canvas.selected_shapes = [shape]
        canvas.show()
        self.app.processEvents()
        before = [(point.x(), point.y()) for point in shape.points]

        canvas._set_image_peek(False)
        self._drag_vertex(
            canvas, QtCore.QPointF(50, 50), QtCore.QPointF(70, 80)
        )
        self.assertNotEqual(
            [(point.x(), point.y()) for point in shape.points], before
        )
        for point, (x, y) in zip(shape.points, before):
            point.setX(x)
            point.setY(y)

        canvas._set_image_peek(True)
        self._drag_vertex(
            canvas, QtCore.QPointF(50, 50), QtCore.QPointF(90, 90)
        )
        self.assertEqual(
            [(point.x(), point.y()) for point in shape.points], before
        )
        self.assertTrue(shape.selected)

        canvas.mode = Canvas.CREATE
        current = Shape(shape_type="line")
        current.line_color = QtGui.QColor(255, 0, 0)
        current.select_line_color = QtGui.QColor(255, 0, 0)
        current.line_width = 4
        current.points = [
            QtCore.QPointF(40, 20),
            QtCore.QPointF(200, 20),
        ]
        canvas.current = current
        canvas.line.points = list(current.points)
        canvas.line.shape_type = "line"
        canvas.line.line_color = QtGui.QColor(255, 0, 0)
        image, ox, oy = self._render(canvas)
        self.assertTrue(self._has_red(image, ox, oy, 16, 28, 60, 180))
        hidden_best, hidden_rows = self._scan_edge(
            image, ox, oy, 42, 62, 80, 160
        )
        self.assertEqual(hidden_rows, 0, hidden_best)
        self.assertEqual(
            [(point.x(), point.y()) for point in shape.points], before
        )

    def test_text_input_and_ctrl_h_do_not_peek(self):
        canvas = self._canvas()
        edit = QtWidgets.QLineEdit()
        edit.show()
        edit.setFocus()
        self.app.processEvents()
        QtTest.QTest.keyClick(edit, QtCore.Qt.Key.Key_H)
        self.app.processEvents()
        self.assertFalse(canvas._image_peek)
        self.assertEqual(edit.text().lower(), "h")
        edit.close()

        event = _key(
            QtCore.QEvent.Type.KeyPress,
            QtCore.Qt.Key.Key_H,
            QtCore.Qt.KeyboardModifier.ControlModifier,
        )
        canvas.keyPressEvent(event)
        self.assertFalse(canvas._image_peek)

    def test_brush_stroke_survives_h_before_and_after_release(self):
        canvas = self._canvas(width=120, height=120)
        shape = self._polygon(selected=True, line_width=2)
        shape.points = [
            QtCore.QPointF(10, 10),
            QtCore.QPointF(50, 10),
            QtCore.QPointF(50, 50),
            QtCore.QPointF(10, 50),
        ]
        shape.close()
        canvas.shapes = [shape]
        canvas.selected_shapes = [shape]
        canvas.store_shapes()
        canvas.show()
        self.app.processEvents()
        canvas.set_brush_mode(True)
        self.assertTrue(canvas.is_brush_mode)
        undo_before = len(canvas._brush_undo_stack)

        canvas.mousePressEvent(self._brush_press(QtCore.QPointF(62, 30)))
        self.assertTrue(canvas._brush_stroke_dirty)
        self.assertLessEqual(max(point.x() for point in shape.points), 50.5)
        self.assertEqual(len(canvas._brush_undo_stack), undo_before)

        canvas.keyPressEvent(
            _key(
                QtCore.QEvent.Type.KeyPress,
                QtCore.Qt.Key.Key_H,
                QtCore.Qt.KeyboardModifier.NoModifier,
            )
        )
        self.assertFalse(canvas._image_peek)
        self.assertTrue(canvas._brush_stroke_dirty)
        self.assertEqual(len(canvas._brush_undo_stack), undo_before)
        self.assertTrue(getattr(shape, "_brush_using_mask", False))

        canvas.mouseReleaseEvent(self._brush_release(QtCore.QPointF(62, 30)))
        self.assertFalse(canvas._brush_stroke_dirty)
        self.assertGreater(max(point.x() for point in shape.points), 55)
        self.assertEqual(len(canvas._brush_undo_stack), undo_before + 1)
        self.assertEqual(shape.shape_type, "polygon")
        committed = [(point.x(), point.y()) for point in shape.points]

        canvas.mouseReleaseEvent(self._brush_release(QtCore.QPointF(62, 30)))
        self.assertEqual(len(canvas._brush_undo_stack), undo_before + 1)
        self.assertEqual(
            [(point.x(), point.y()) for point in shape.points], committed
        )

        canvas.keyPressEvent(
            _key(
                QtCore.QEvent.Type.KeyPress,
                QtCore.Qt.Key.Key_H,
                QtCore.Qt.KeyboardModifier.NoModifier,
            )
        )
        self.assertTrue(canvas._image_peek)
        self.assertFalse(canvas._brush_stroke_dirty)
        self.assertEqual(len(canvas._brush_undo_stack), undo_before + 1)
        self.assertEqual(
            [(point.x(), point.y()) for point in shape.points], committed
        )
        canvas.keyReleaseEvent(
            _key(
                QtCore.QEvent.Type.KeyRelease,
                QtCore.Qt.Key.Key_H,
                QtCore.Qt.KeyboardModifier.NoModifier,
            )
        )
        self.assertFalse(canvas._image_peek)
        self.assertEqual(
            [(point.x(), point.y()) for point in shape.points], committed
        )

    def test_release_still_commits_when_peek_is_already_on(self):
        canvas = self._canvas(width=120, height=120)
        shape = self._polygon(selected=True, line_width=2)
        shape.points = [
            QtCore.QPointF(10, 10),
            QtCore.QPointF(50, 10),
            QtCore.QPointF(50, 50),
            QtCore.QPointF(10, 50),
        ]
        shape.close()
        canvas.shapes = [shape]
        canvas.selected_shapes = [shape]
        canvas.store_shapes()
        canvas.show()
        self.app.processEvents()
        canvas.set_brush_mode(True)
        undo_before = len(canvas._brush_undo_stack)
        canvas.mousePressEvent(self._brush_press(QtCore.QPointF(62, 30)))
        canvas._image_peek = True

        canvas.mouseReleaseEvent(self._brush_release(QtCore.QPointF(62, 30)))

        self.assertFalse(canvas._brush_stroke_dirty)
        self.assertEqual(len(canvas._brush_undo_stack), undo_before + 1)
        self.assertGreater(max(point.x() for point in shape.points), 55)
        self.assertEqual(shape.shape_type, "polygon")

    def test_ctrl_h_is_not_a_peek_and_still_triggers_its_shortcut(self):
        window = QtWidgets.QWidget()
        window.resize(240, 240)
        canvas = Canvas(parent=window, mask={"opacity": 80})
        canvas.setParent(window)
        canvas.resize(240, 240)
        self.canvases.append(canvas)
        triggered = []
        action = QtGui.QAction(window)
        action.setShortcut(QtGui.QKeySequence("Ctrl+H"))
        action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        action.triggered.connect(lambda: triggered.append("ctrl-h"))
        window.addAction(action)
        window.show()
        canvas.show()
        canvas.setFocus()
        self.app.processEvents()

        QtTest.QTest.keyClick(
            canvas,
            QtCore.Qt.Key.Key_H,
            QtCore.Qt.KeyboardModifier.ControlModifier,
        )
        self.app.processEvents()
        self.assertEqual(triggered, ["ctrl-h"])
        self.assertFalse(canvas._image_peek)

        canvas._set_image_peek(True)
        press = _key(
            QtCore.QEvent.Type.KeyPress,
            QtCore.Qt.Key.Key_H,
            QtCore.Qt.KeyboardModifier.ControlModifier,
        )
        canvas.keyPressEvent(press)
        self.assertFalse(press.isAccepted())
        self.assertTrue(canvas._image_peek)

        QtTest.QTest.keyClick(
            canvas,
            QtCore.Qt.Key.Key_H,
            QtCore.Qt.KeyboardModifier.ControlModifier,
        )
        self.app.processEvents()
        self.assertEqual(triggered, ["ctrl-h", "ctrl-h"])
        self.assertFalse(canvas._image_peek)
        window.close()

    def test_visualization_keeps_normal_strokes_for_closed_contours(self):
        boxes = {
            "rectangle": [(50, 50), (190, 50), (190, 150), (50, 150)],
            "rotation": [(50, 50), (190, 50), (190, 150), (50, 150)],
            "quadrilateral": [(50, 50), (190, 50), (190, 150), (50, 150)],
        }
        for kind, points in boxes.items():
            self._assert_export_keeps_width(kind, points, circle=False)
        self._assert_export_keeps_width(
            "circle",
            [(120, 100), (120, 60)],
            circle=True,
        )

    def _assert_export_keeps_width(self, kind, points, circle):
        canvas = self._canvas()
        shape = Shape(label="contour", shape_type=kind)
        shape.points = [QtCore.QPointF(x, y) for x, y in points]
        if kind != "circle":
            shape.close()
        shape.line_color = QtGui.QColor(0, 0, 255)
        shape.select_line_color = QtGui.QColor(0, 0, 255)
        shape.line_width = 8
        shape.point_size = 2
        shape.selected = True
        shape.visible = True
        canvas.shapes = [shape]
        canvas.selected_shapes = [shape]
        canvas.edit_outline_dashed = True
        before = self._snapshot(shape)
        edited, ox, oy = self._render(canvas)
        if circle:
            edited_ink = self._max_blue(edited, ox, oy, 92, 108, 64, 96)
        else:
            _best, edited_ink = self._scan_edge(
                edited, ox, oy, 42, 62, 80, 160
            )
        self.assertLessEqual(edited_ink, 4, (kind, edited_ink))

        Shape.edit_outline_dashed = False
        exported = canvas.render_visualization(
            canvas.pixmap,
            [shape],
            show_labels=False,
            show_texts=False,
            show_masks=True,
            show_scores=False,
            show_groups=False,
        )
        if circle:
            exported_ink = self._max_blue(exported, 0, 0, 92, 108, 64, 96)
        else:
            best, exported_ink = self._scan_edge(
                exported, 0, 0, 42, 62, 80, 160
            )
            self.assertLessEqual(best[1], 2, (kind, best))
        self.assertGreaterEqual(exported_ink, 6, (kind, exported_ink))
        self.assertFalse(Shape.edit_outline_dashed)
        self.assertFalse(getattr(shape, "_use_edit_outline", False))
        self.assertEqual(self._snapshot(shape), before)
        self.assertEqual(shape.line_width, 8)

    def _max_blue(self, image, ox, oy, y0, y1, x0, x1):
        best = 0
        for y in range(y0, y1 + 1):
            count = 0
            for x in range(x0, x1 + 1):
                if _is_blue(self._color(image, x, y, ox, oy)):
                    count += 1
            best = max(best, count)
        return best

    def _brush_press(self, pos):
        return _mouse(
            QtCore.QEvent.Type.MouseButtonPress,
            pos,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.LeftButton,
        )

    def _brush_release(self, pos):
        return _mouse(
            QtCore.QEvent.Type.MouseButtonRelease,
            pos,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.NoButton,
        )

    def _drag_vertex(self, canvas, start, end):
        canvas.mouseMoveEvent(
            _mouse(
                QtCore.QEvent.Type.MouseMove,
                start,
                QtCore.Qt.MouseButton.NoButton,
                QtCore.Qt.MouseButton.NoButton,
            )
        )
        canvas.mousePressEvent(
            _mouse(
                QtCore.QEvent.Type.MouseButtonPress,
                start,
                QtCore.Qt.MouseButton.LeftButton,
                QtCore.Qt.MouseButton.LeftButton,
            )
        )
        canvas.mouseMoveEvent(
            _mouse(
                QtCore.QEvent.Type.MouseMove,
                end,
                QtCore.Qt.MouseButton.LeftButton,
                QtCore.Qt.MouseButton.LeftButton,
            )
        )
        canvas.mouseReleaseEvent(
            _mouse(
                QtCore.QEvent.Type.MouseButtonRelease,
                end,
                QtCore.Qt.MouseButton.LeftButton,
                QtCore.Qt.MouseButton.NoButton,
            )
        )

    def _has_green(self, image, ox, oy, y0, y1, x0, x1):
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                color = self._color(image, x, y, ox, oy)
                if (
                    color.green() > 150
                    and color.green() > color.blue() + 40
                    and color.green() > color.red() + 40
                ):
                    return True
        return False

    def _has_red(self, image, ox, oy, y0, y1, x0, x1):
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                color = self._color(image, x, y, ox, oy)
                if (
                    color.red() > 150
                    and color.red() > color.blue() + 40
                    and color.red() > color.green() + 40
                ):
                    return True
        return False


if __name__ == "__main__":
    unittest.main()

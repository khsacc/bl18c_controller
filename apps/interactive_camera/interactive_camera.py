import csv
import cv2
import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets
import math
import os
import sys
import threading
import json
import tempfile
import shutil
from datetime import datetime
try:
    from .autofocus import AutoFocus
    from .sample_tracking import compute_xy_shift, compute_similarity
except ImportError:
    from apps.interactive_camera.autofocus import AutoFocus
    from apps.interactive_camera.sample_tracking import compute_xy_shift, compute_similarity

try:
    from utils.stage.control_stage import PM16CController, PULSE_SCALE
    from settings import log_prefs
except ImportError:
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from utils.stage.control_stage import PM16CController, PULSE_SCALE
    from settings import log_prefs

try:
    from settings.i18n import tr
except ImportError:
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from settings.i18n import tr

UM_PER_PULSE_CH3 = PULSE_SCALE[3]
UM_PER_PULSE_CH4 = PULSE_SCALE[4]
UM_PER_PULSE_CH5 = PULSE_SCALE[5]
UM_PER_PULSE_CH7 = PULSE_SCALE[7]


def _no_wheel(widget):
    """Ignore mouse-wheel events on spin/combo boxes so scrolling the panel
    never silently changes a value the cursor happens to be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


class RadiusPopup(QtWidgets.QWidget):
    """Small floating popup for adjusting a selected circle's radius in real-time."""

    def __init__(self, parent=None):
        super().__init__(
            parent,
            QtCore.Qt.WindowType.Tool | QtCore.Qt.WindowType.FramelessWindowHint,
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(
            "background-color: #f0f0f0; border: 1px solid #888; border-radius: 4px;"
        )

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        layout.addWidget(QtWidgets.QLabel(tr("r (px):")))
        self.spinbox = _no_wheel(QtWidgets.QSpinBox())
        self.spinbox.setRange(1, 9999)
        self.spinbox.setFixedWidth(70)
        self.spinbox.setFocusPolicy(QtCore.Qt.FocusPolicy.ClickFocus)
        layout.addWidget(self.spinbox)
        self.adjustSize()

        self._shapes = None
        self._idx = -1
        self._updating = False
        self.spinbox.valueChanged.connect(self._on_value_changed)

    def attach(self, shapes, idx):
        self._shapes = shapes
        self._idx = idx
        self._updating = True
        self.spinbox.setValue(shapes[idx]['r'])
        self._updating = False

    def detach(self):
        self._shapes = None
        self._idx = -1

    def _on_value_changed(self, value):
        if self._updating or self._shapes is None or self._idx < 0:
            return
        if self._idx < len(self._shapes) and self._shapes[self._idx].get('type') in ('circle', 'cross'):
            self._shapes[self._idx]['r'] = value


class CalibrationDialog(QtWidgets.QDialog):
    """3-step calibration that builds a 2x2 forward matrix M where
    [Δpx, Δpy]^T = M @ [ΔCh4, ΔCh5]^T, correcting for camera-stage axis tilt."""

    def __init__(self, controller, parent=None, *, motion=None):
        super().__init__(parent)
        self.controller = controller
        self.motion = motion  # MotionLease acquired by the parent window
        self.motor_positions = [None, None, None]   # (ch4, ch5) for steps 0-2
        self.pixel_positions = [None, None, None]   # (px, py) for steps 0-2
        self.current_step = 0
        self.calibration_data = None
        self.waiting_for_pixel_click = False

        self.setWindowTitle(tr("Stage Calibration"))
        self.setFixedSize(520, 500)

        instructions = QtWidgets.QTextEdit(self)
        instructions.setReadOnly(True)
        instructions.setPlainText(
            tr("2×2 matrix calibration — corrects for camera/stage axis tilt:\n\n"
               "Step 1: Click 'Record Origin', then click the reference point on the camera image.\n"
               "Step 2: Move ONLY Ch4 (any amount), click 'Record Ch4-Moved', then click reference point.\n"
               "Step 3: Move ONLY Ch5 (Ch4 unchanged), click 'Record Ch5-Moved', then click reference point.\n"
               "Step 4: Click 'Calculate Calibration', then close this window.")
        )
        instructions.setFixedHeight(110)

        self.btn_origin = QtWidgets.QPushButton(tr("1. Record Origin Motor Position"))
        self.btn_ch4 = QtWidgets.QPushButton(tr("2. Record Ch4-Moved Position  (move Ch4 only first)"))
        self.btn_ch5 = QtWidgets.QPushButton(tr("3. Record Ch5-Moved Position  (move Ch5 only, from step 2 position)"))
        self.btn_calculate = QtWidgets.QPushButton(tr("4. Calculate Calibration"))

        self.btn_ch4.setEnabled(False)
        self.btn_ch5.setEnabled(False)
        self.btn_calculate.setEnabled(False)

        self.status_label = QtWidgets.QLabel(tr("Step 1: Click 'Record Origin Motor Position'."))
        self.status_label.setWordWrap(True)

        self.btn_origin.clicked.connect(lambda: self._record_motor(0))
        self.btn_ch4.clicked.connect(lambda: self._record_motor(1))
        self.btn_ch5.clicked.connect(lambda: self._record_motor(2))
        self.btn_calculate.clicked.connect(self.calculate_calibration)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(tr("Calibration Procedure:")))
        layout.addWidget(instructions)
        layout.addWidget(self.btn_origin)
        layout.addWidget(self.btn_ch4)
        layout.addWidget(self.btn_ch5)
        layout.addWidget(self.btn_calculate)
        layout.addStretch()
        layout.addWidget(self.status_label)

    def _record_motor(self, step):
        ch4_pos = self.controller.get_ch_pos(4)
        ch5_pos = self.controller.get_ch_pos(5)
        if ch4_pos is None or ch5_pos is None:
            QtWidgets.QMessageBox.critical(self, tr("Error"), tr("Could not read motor positions."))
            return
        try:
            self.motor_positions[step] = (int(ch4_pos), int(ch5_pos))
        except ValueError:
            QtWidgets.QMessageBox.critical(self, tr("Error"), tr("Could not read motor positions."))
            return

        self.current_step = step
        self.waiting_for_pixel_click = True
        [self.btn_origin, self.btn_ch4, self.btn_ch5][step].setEnabled(False)

        labels = [tr("Origin"), tr("Ch4-moved"), tr("Ch5-moved")]
        m = self.motor_positions[step]
        self.status_label.setText(
            tr("{label} motor recorded: Ch4={ch4}, Ch5={ch5}.\n"
               "Now, click the reference point on the camera image.",
               label=labels[step], ch4=m[0], ch5=m[1])
        )

    def record_pixel_position(self, x, y):
        if not self.waiting_for_pixel_click:
            return
        step = self.current_step
        self.pixel_positions[step] = (x, y)
        self.waiting_for_pixel_click = False

        if step == 0:
            self.status_label.setText(
                tr("Origin pixel ({x}, {y}) recorded.\n"
                   "Move ONLY Ch4 a noticeable amount, then click 'Record Ch4-Moved Position'.",
                   x=x, y=y)
            )
            self.btn_ch4.setEnabled(True)
            # LOC so the user can jog Ch4 manually between calibration steps;
            # the lease stays held (parent releases it when the dialog closes).
            try:
                self.controller.switch_to_loc(motion=self.motion)
            except Exception:
                pass
        elif step == 1:
            self.status_label.setText(
                tr("Ch4-moved pixel ({x}, {y}) recorded.\n"
                   "From current position move ONLY Ch5, then click 'Record Ch5-Moved Position'.",
                   x=x, y=y)
            )
            self.btn_ch5.setEnabled(True)
        elif step == 2:
            self.status_label.setText(
                tr("Ch5-moved pixel ({x}, {y}) recorded.\n"
                   "Click 'Calculate Calibration' to finish.",
                   x=x, y=y)
            )
            self.btn_calculate.setEnabled(True)

    def calculate_calibration(self):
        if any(p is None for p in self.motor_positions) or any(p is None for p in self.pixel_positions):
            QtWidgets.QMessageBox.critical(self, tr("Error"), tr("Complete all three recording steps first."))
            return

        m0, m1, m2 = self.motor_positions
        p0, p1, p2 = self.pixel_positions

        # Step 0→1: only Ch4 moved
        d_ch4_01 = m1[0] - m0[0]
        # Step 1→2: only Ch5 moved (from step-1 position)
        d_ch5_12 = m2[1] - m1[1]

        if abs(d_ch4_01) < 1:
            QtWidgets.QMessageBox.critical(
                self, tr("Error"),
                tr("Ch4 barely moved between steps 1 and 2.\nMove Ch4 more and redo step 2.")
            )
            return
        if abs(d_ch5_12) < 1:
            QtWidgets.QMessageBox.critical(
                self, tr("Error"),
                tr("Ch5 barely moved between steps 2 and 3.\nMove Ch5 more and redo step 3.")
            )
            return

        # Forward matrix M: [Δpx, Δpy]^T = M @ [ΔCh4, ΔCh5]^T
        a = (p1[0] - p0[0]) / d_ch4_01   # Δpx per ΔCh4
        c = (p1[1] - p0[1]) / d_ch4_01   # Δpy per ΔCh4
        b = (p2[0] - p1[0]) / d_ch5_12   # Δpx per ΔCh5
        d = (p2[1] - p1[1]) / d_ch5_12   # Δpy per ΔCh5

        M = np.array([[a, b], [c, d]])
        det = a * d - b * c
        if abs(det) < 1e-12:
            QtWidgets.QMessageBox.critical(
                self, tr("Error"),
                tr("Calibration matrix is singular — Ch4 and Ch5 appear to move in the same direction.\n"
                   "Ensure step 2 moves ONLY Ch4 and step 3 moves ONLY Ch5.")
            )
            return

        M_inv = np.linalg.inv(M)
        angle_ch4 = math.degrees(math.atan2(c, a))
        angle_ch5 = math.degrees(math.atan2(d, b))

        self.calibration_data = {
            'matrix': M.tolist(),
            'matrix_inv': M_inv.tolist(),
        }

        self.status_label.setText(
            tr("Calibration complete!\n"
               "Ch4 axis: {ch4:.1f}° from camera X-axis\n"
               "Ch5 axis: {ch5:.1f}° from camera X-axis",
               ch4=angle_ch4, ch5=angle_ch5)
        )
        QtWidgets.QMessageBox.information(
            self, tr("Calibration"),
            tr("Calibration completed!\n"
               "Ch4 axis: {ch4:.1f}°  Ch5 axis: {ch5:.1f}°\n\n"
               "Close this window to save and apply the resultant calibration.",
               ch4=angle_ch4, ch5=angle_ch5)
        )

    def get_calibration(self):
        return self.calibration_data


class AutoFocusSettingsDialog(QtWidgets.QDialog):
    """Settings > Auto Focus — global sharpness-metric / peak-detection choice,
    shared by the Interactive Camera tab, Sample Tracking tab, and the hidden
    Ch7 autofocus (which copies these from the Ch3 AutoFocus before each run).
    Session-only: not persisted, resets to default (Tenengrad / Gaussian fit)
    on app restart."""

    def __init__(self, current_method, current_peak_method, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Auto Focus Settings"))
        self.method = current_method
        self.peak_method = current_peak_method

        method_group_box = QtWidgets.QGroupBox(tr("Sharpness Method"))
        method_layout = QtWidgets.QVBoxLayout(method_group_box)
        self.method_group = QtWidgets.QButtonGroup(self)
        self.method_tenengrad = QtWidgets.QRadioButton(tr("Tenengrad"))
        self.method_laplacian = QtWidgets.QRadioButton(tr("Laplacian"))
        self.method_group.addButton(self.method_tenengrad, 0)
        self.method_group.addButton(self.method_laplacian, 1)
        method_layout.addWidget(self.method_tenengrad)
        method_layout.addWidget(self.method_laplacian)
        (self.method_tenengrad if current_method == 'tenengrad' else self.method_laplacian).setChecked(True)

        peak_group_box = QtWidgets.QGroupBox(tr("Peak Detection"))
        peak_layout = QtWidgets.QVBoxLayout(peak_group_box)
        self.peak_group = QtWidgets.QButtonGroup(self)
        self.peak_gaussian = QtWidgets.QRadioButton(tr("Gaussian fit"))
        self.peak_highest = QtWidgets.QRadioButton(tr("Highest"))
        self.peak_group.addButton(self.peak_gaussian, 0)
        self.peak_group.addButton(self.peak_highest, 1)
        peak_layout.addWidget(self.peak_gaussian)
        peak_layout.addWidget(self.peak_highest)
        (self.peak_gaussian if current_peak_method == 'gaussian' else self.peak_highest).setChecked(True)

        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(method_group_box)
        layout.addWidget(peak_group_box)
        layout.addWidget(button_box)

    def accept(self):
        self.method = 'tenengrad' if self.method_tenengrad.isChecked() else 'laplacian'
        self.peak_method = 'gaussian' if self.peak_gaussian.isChecked() else 'highest'
        super().accept()


class VideoLabel(QtWidgets.QLabel):
    left_clicked = QtCore.pyqtSignal(QtCore.QPoint)
    right_clicked = QtCore.pyqtSignal(QtCore.QPoint)
    mouse_moved = QtCore.pyqtSignal(QtCore.QPoint)
    left_released = QtCore.pyqtSignal(QtCore.QPoint)
    wheel_scrolled = QtCore.pyqtSignal(int)  # +1 = up (enlarge), -1 = down (shrink)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.left_clicked.emit(event.position().toPoint())
        elif event.button() == QtCore.Qt.MouseButton.RightButton:
            self.right_clicked.emit(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self.mouse_moved.emit(event.position().toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.left_released.emit(event.position().toPoint())
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta != 0:
            self.wheel_scrolled.emit(1 if delta > 0 else -1)
        event.accept()


class MainWindow(QtWidgets.QMainWindow):
    _tracking_log_signal = QtCore.pyqtSignal(str)

    def __init__(self, controller=None):
        super().__init__()
        self.setWindowTitle(tr("Interactive Camera Stage Control"))
        self.resize(1000, 780)

        if controller is not None:
            self.controller = controller
            self._owns_controller = False
        else:
            self.controller = PM16CController(ip='192.168.1.55', port=7777, debug=True)
            try:
                self.controller.connect()
            except Exception as exc:
                QtWidgets.QMessageBox.critical(self, tr("Connection Error"), tr("Could not connect to motor controller: {error}", error=exc))
                raise
            self._owns_controller = True

        self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            QtWidgets.QMessageBox.critical(self, tr("Camera Error"), tr("Could not open camera."))
            if self._owns_controller:
                self.controller.disconnect()
            raise RuntimeError("Could not open camera.")

        self._cap_lock = threading.Lock()
        self.autofocus = AutoFocus(self.controller, self.cap, focus_range=20, step_size=2,
                                   method='tenengrad', peak_method='gaussian', n_frames=10,
                                   cap_lock=self._cap_lock)
        self.autofocus_ch7 = AutoFocus(self.controller, self.cap, focus_range=20, step_size=2,
                                       method='tenengrad', peak_method='gaussian', n_frames=10,
                                       channel=7, cap_lock=self._cap_lock)
        self.frame_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.frame_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        default_fps = self.cap.get(cv2.CAP_PROP_FPS)
        if default_fps == 0.0 or default_fps == -1.0:
            default_fps = 30.0
        self.fps = default_fps

        # --- Shape annotation state ---
        # Each shape: {'type': 'circle'|'rect'|'line', ...type-specific coords...}
        # circle: cx, cy, r
        # rect:   x1, y1, x2, y2
        # line:   x1, y1, x2, y2
        self.shapes = []
        self.selected_idx = -1
        self.draw_mode = None        # 'circle', 'rect', 'line', or None
        self.draw_preview = None     # partial shape dict during active drawing
        self.drag_start_frame = None # (fx, fy) in frame coords at mouse press
        self.line_first_point = None # (fx, fy) of first click in line mode
        self.is_dragging_shape = False
        self.drag_shape_origin = None  # snapshot of shape at drag start

        self.show_all_marks = True
        self.show_timestamp = True
        self.is_recording = False
        self.calibration_mode = False
        self.calibration_dialog = None
        self.calibration_data = None
        self.click_to_move_enabled = False
        self.render_params = {'scale': 1.0, 'dx': 0, 'dy': 0}
        self.current_frame = None
        self.is_moving = False
        self._move_thread = None
        self.calibration_filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
        self.last_save_dir = os.path.expanduser("~")
        self._localdata_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__localdata")
        self._cache_path = os.path.join(self._localdata_dir, "camera_cache.json")
        self._cached_tracking_log_dir = None
        self._load_localdata_cache()
        self.laser_spot_pos = None  # (fx, fy) in frame coords
        self._laser_spot_path = os.path.join(self._localdata_dir, "laser_spot.json")
        self._load_laser_spot()
        self._shapes_path = os.path.join(self._localdata_dir, "shapes.json")
        self._load_shapes()
        self.xray_beam_pos = None  # (fx, fy) in frame coords — session only, not persisted
        self.video_writer = None
        self.video_temp_path = None

        # Sample tracking state
        self.reference_frame = None
        self.is_following = False
        self.is_following_running = False
        self._follow_thread = None
        self.follow_cumulative = {3: 0, 4: 0, 5: 0}
        self.follow_origin_pos = {}
        self.tracking_csv_file = None
        self.tracking_csv_writer = None
        self.tracking_csv_path = None
        self.tracking_start_time = None
        self.tracking_images_dir = None
        self.tracking_image_counter = 0
        self._follow_stop_reason = None
        self._af_syncing = False

        if os.path.exists(self.calibration_filepath):
            try:
                with open(self.calibration_filepath, 'r') as f:
                    self.calibration_data = json.load(f)
                # Convert old scale_x/scale_y format to 2x2 matrix format
                if 'matrix_inv' not in self.calibration_data:
                    sx = self.calibration_data.get('scale_x', 0)
                    sy = self.calibration_data.get('scale_y', 0)
                    if sx and sy:
                        M_inv = np.array([[sx, 0.0], [0.0, sy]])
                        self.calibration_data['matrix'] = np.linalg.inv(M_inv).tolist()
                        self.calibration_data['matrix_inv'] = M_inv.tolist()
                print("Loaded existing calibration data from JSON.")
            except Exception as e:
                print(f"Failed to load calibration data: {e}")

        # --- VideoLabel ---
        self.video_label = VideoLabel(self)
        self.video_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet("background-color: black;")
        self.video_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Ignored,
        )
        self.video_label.left_clicked.connect(self.on_video_left_click)
        self.video_label.right_clicked.connect(self.on_video_right_click)
        self.video_label.mouse_moved.connect(self.on_video_mouse_move)
        self.video_label.left_released.connect(self.on_video_left_release)
        self.video_label.wheel_scrolled.connect(self.on_video_wheel)

        # --- Radius popup (shown when a circle is selected) ---
        self.radius_popup = RadiusPopup(self)
        self.radius_popup.hide()

        # --- Control bar ---
        self.btn_calibrate = QtWidgets.QPushButton(tr("Calibrate"))
        self.click_to_move_checkbox = QtWidgets.QCheckBox(tr("Enable Click-to-Move"))
        if self.calibration_data is None:
            self.click_to_move_checkbox.setEnabled(False)
        self.click_to_move_checkbox.toggled.connect(self.on_click_to_move_toggled)
        self.ctm_notice_label = QtWidgets.QLabel(
            tr("Click-to-Move is ON: clicking on the image will move Ch4/5 to centre that position."))
        self.ctm_notice_label.setStyleSheet("color: orange; font-weight: bold;")
        self.ctm_notice_label.setVisible(False)

        self.ctm_speed_group = QtWidgets.QButtonGroup(self)
        self.ctm_speed_h = QtWidgets.QRadioButton("H")
        self.ctm_speed_m = QtWidgets.QRadioButton("M")
        self.ctm_speed_l = QtWidgets.QRadioButton("L")
        self.ctm_speed_m.setChecked(True)
        self.ctm_speed_group.addButton(self.ctm_speed_h, 0)
        self.ctm_speed_group.addButton(self.ctm_speed_m, 1)
        self.ctm_speed_group.addButton(self.ctm_speed_l, 2)

        self.btn_auto_focus = QtWidgets.QPushButton(tr("Start Auto-Focus"))
        self.btn_stop_focus = QtWidgets.QPushButton(tr("Stop Auto-Focus"))

        self.autofocus_status_label = QtWidgets.QLabel(tr("Auto focusing in progress. Ch3 is moving."))
        self.autofocus_status_label.setStyleSheet("color: red; font-weight: bold;")
        self.autofocus_status_label.setVisible(False)

        self.focus_range_label = QtWidgets.QLabel(tr("Scan Range (um):"))
        self.focus_range_spinbox = _no_wheel(QtWidgets.QSpinBox())
        self.focus_range_spinbox.setRange(2, 2000)
        self.focus_range_spinbox.setSingleStep(2)
        self.focus_range_spinbox.setValue(60)
        self.focus_range_spinbox.setToolTip(tr("±scan range in um (1 pulse = 2 um)"))

        self.focus_step_label = QtWidgets.QLabel(tr("Step (pulse):"))
        self.focus_step_spinbox = _no_wheel(QtWidgets.QSpinBox())
        self.focus_step_spinbox.setRange(1, 10)
        self.focus_step_spinbox.setValue(4)
        self.focus_step_spinbox.setToolTip(tr("Ch3 step size per scan position (pulses). 1 pulse = 2 um."))
        self.focus_step_spinbox.valueChanged.connect(self._on_focus_step_changed)

        self.focus_nframes_label = QtWidgets.QLabel(tr("Frames/pos:"))
        self.focus_nframes_spinbox = _no_wheel(QtWidgets.QSpinBox())
        self.focus_nframes_spinbox.setRange(1, 50)
        self.focus_nframes_spinbox.setValue(10)
        self.focus_nframes_spinbox.setToolTip(
            tr("Number of frames averaged per scan position.\n"
               "1 = no averaging (default). Higher values reduce noise but slow the scan."))
        self.focus_nframes_spinbox.valueChanged.connect(self._on_focus_nframes_changed)

        self.focus_speed_group = QtWidgets.QButtonGroup(self)
        self.focus_speed_h = QtWidgets.QRadioButton("H")
        self.focus_speed_m = QtWidgets.QRadioButton("M")
        self.focus_speed_l = QtWidgets.QRadioButton("L")
        self.focus_speed_h.setChecked(True)
        self.focus_speed_group.addButton(self.focus_speed_h, 0)
        self.focus_speed_group.addButton(self.focus_speed_m, 1)
        self.focus_speed_group.addButton(self.focus_speed_l, 2)

        self.btn_snapshot = QtWidgets.QPushButton(tr("Snapshot"))
        self.btn_start_record = QtWidgets.QPushButton(tr("Start Recording"))
        self.btn_stop_record = QtWidgets.QPushButton(tr("Stop Recording"))
        self.btn_stop_record.setEnabled(False)

        self.btn_toggle_marks = QtWidgets.QPushButton(tr("Show/Hide Marks"))
        self.btn_toggle_timestamp = QtWidgets.QPushButton(tr("Show/Hide Timestamp"))

        # --- Drawing toolbar buttons ---
        icon_font = QtGui.QFont()
        icon_font.setPointSize(14)

        self.btn_draw_circle = QtWidgets.QToolButton()
        self.btn_draw_circle.setText("○")
        self.btn_draw_circle.setFont(icon_font)
        self.btn_draw_circle.setCheckable(True)
        self.btn_draw_circle.setFixedSize(36, 36)
        self.btn_draw_circle.setToolTip(tr("Circle — drag from centre outward"))
        self.btn_draw_circle.clicked.connect(lambda: self._set_draw_mode('circle'))

        self.btn_draw_rect = QtWidgets.QToolButton()
        self.btn_draw_rect.setText("□")
        self.btn_draw_rect.setFont(icon_font)
        self.btn_draw_rect.setCheckable(True)
        self.btn_draw_rect.setFixedSize(36, 36)
        self.btn_draw_rect.setToolTip(tr("Rectangle — drag corner to corner"))
        self.btn_draw_rect.clicked.connect(lambda: self._set_draw_mode('rect'))

        self.btn_draw_line = QtWidgets.QToolButton()
        self.btn_draw_line.setText("╱")
        self.btn_draw_line.setFont(icon_font)
        self.btn_draw_line.setCheckable(True)
        self.btn_draw_line.setFixedSize(36, 36)
        self.btn_draw_line.setToolTip(tr("Line — click two points (hold Shift for H/V/45°)"))
        self.btn_draw_line.clicked.connect(lambda: self._set_draw_mode('line'))

        self.btn_draw_cross = QtWidgets.QToolButton()
        self.btn_draw_cross.setText("✛")
        self.btn_draw_cross.setFont(icon_font)
        self.btn_draw_cross.setCheckable(True)
        self.btn_draw_cross.setFixedSize(36, 36)
        self.btn_draw_cross.setToolTip(tr("Cross/Crosshair — drag from centre outward"))
        self.btn_draw_cross.clicked.connect(lambda: self._set_draw_mode('cross'))

        # --- Stage Control (Relative Move) widgets ---
        _laser_text = (tr("Laser position: ({x}, {y})",
                          x=self.laser_spot_pos[0], y=self.laser_spot_pos[1])
                       if self.laser_spot_pos else tr("Laser position: unregistered"))
        self.laser_spot_label = QtWidgets.QLabel(_laser_text)
        self.xray_beam_label = QtWidgets.QLabel(tr("x-ray beam position: unregistered"))

        self.rel_step_ch3 = _no_wheel(QtWidgets.QSpinBox())
        self.rel_step_ch3.setRange(1, 9999)
        self.rel_step_ch3.setValue(10)
        self.rel_step_ch3.setMaximumWidth(70)
        self.rel_step_ch3.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)

        self.rel_step_ch4 = _no_wheel(QtWidgets.QSpinBox())
        self.rel_step_ch4.setRange(1, 9999)
        self.rel_step_ch4.setValue(10)
        self.rel_step_ch4.setMaximumWidth(70)
        self.rel_step_ch4.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)

        self.rel_step_ch5 = _no_wheel(QtWidgets.QSpinBox())
        self.rel_step_ch5.setRange(1, 9999)
        self.rel_step_ch5.setValue(10)
        self.rel_step_ch5.setMaximumWidth(70)
        self.rel_step_ch5.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)

        self.status_label = QtWidgets.QLabel(tr("Ready."))
        self.status_label.setWordWrap(True)

        # --- Wire up non-drawing buttons ---
        self.btn_calibrate.clicked.connect(self.open_calibration_dialog)
        self.btn_auto_focus.clicked.connect(self.start_autofocus)
        self.btn_stop_focus.clicked.connect(self.stop_autofocus)
        self.focus_range_spinbox.valueChanged.connect(self._on_focus_range_changed)
        for _spd_btn in (self.focus_speed_h, self.focus_speed_m, self.focus_speed_l):
            _spd_btn.toggled.connect(lambda checked: self._on_focus_speed_changed() if checked else None)
        self.btn_toggle_marks.clicked.connect(self.toggle_marks)
        self.btn_toggle_timestamp.clicked.connect(self.toggle_timestamp)
        self.btn_snapshot.clicked.connect(self.take_snapshot)
        self.btn_start_record.clicked.connect(self.start_recording)
        self.btn_stop_record.clicked.connect(self.stop_recording)

        # --- Layouts ---

        # [Calibration] group
        calib_group = QtWidgets.QGroupBox(tr("Calibration"))
        calib_inner = QtWidgets.QVBoxLayout(calib_group)
        calib_row1 = QtWidgets.QHBoxLayout()
        calib_row1.addWidget(self.btn_calibrate)
        calib_row1.addWidget(self.click_to_move_checkbox)
        calib_row1.addWidget(QtWidgets.QLabel(tr("Ch4/5 Speed:")))
        calib_row1.addWidget(self.ctm_speed_h)
        calib_row1.addWidget(self.ctm_speed_m)
        calib_row1.addWidget(self.ctm_speed_l)
        calib_row1.addStretch()
        calib_row2 = QtWidgets.QHBoxLayout()
        calib_row2.addWidget(self.laser_spot_label)
        calib_row2.addSpacing(20)
        calib_row2.addWidget(self.xray_beam_label)
        calib_row2.addStretch()
        calib_inner.addLayout(calib_row1)
        calib_inner.addLayout(calib_row2)

        # [Stage Control] group — Move Relative for Ch3/4/5
        stage_ctrl_group = QtWidgets.QGroupBox(tr("Stage Control (Relative Move)"))
        stage_ctrl_inner = QtWidgets.QHBoxLayout(stage_ctrl_group)
        for ch, spin in [(3, self.rel_step_ch3), (4, self.rel_step_ch4), (5, self.rel_step_ch5)]:
            stage_ctrl_inner.addWidget(QtWidgets.QLabel(tr("Ch{ch}:", ch=ch)))
            btn_m = QtWidgets.QToolButton()
            btn_m.setText("−")
            btn_p = QtWidgets.QToolButton()
            btn_p.setText("+")
            for _btn in (btn_m, btn_p):
                _btn.setFixedSize(36, 36)
                _font = _btn.font()
                _font.setPointSize(14)
                _btn.setFont(_font)
            btn_m.clicked.connect(lambda _, c=ch, s=spin: self._move_relative_ch(c, -s.value()))
            btn_p.clicked.connect(lambda _, c=ch, s=spin: self._move_relative_ch(c, s.value()))
            stage_ctrl_inner.addWidget(btn_m)
            stage_ctrl_inner.addWidget(spin)
            stage_ctrl_inner.addWidget(btn_p)
            stage_ctrl_inner.addSpacing(16)
        stage_ctrl_inner.addStretch()

        # [Auto-Focus] group
        af_group = QtWidgets.QGroupBox(tr("Auto-Focus (Ch3)"))
        af_inner = QtWidgets.QVBoxLayout(af_group)

        af_row1 = QtWidgets.QHBoxLayout()
        af_row1.addWidget(self.focus_range_label)
        af_row1.addWidget(self.focus_range_spinbox)
        af_row1.addWidget(self.focus_step_label)
        af_row1.addWidget(self.focus_step_spinbox)
        af_row1.addWidget(self.focus_nframes_label)
        af_row1.addWidget(self.focus_nframes_spinbox)
        af_row1.addStretch()

        af_row1b = QtWidgets.QHBoxLayout()
        af_row1b.addWidget(QtWidgets.QLabel(tr("Speed:")))
        af_row1b.addWidget(self.focus_speed_h)
        af_row1b.addWidget(self.focus_speed_m)
        af_row1b.addWidget(self.focus_speed_l)
        af_row1b.addStretch()

        af_row2 = QtWidgets.QHBoxLayout()
        af_row2.addWidget(self.btn_auto_focus)
        af_row2.addWidget(self.btn_stop_focus)
        af_row2.addWidget(self.autofocus_status_label)
        af_row2.addStretch()

        af_inner.addLayout(af_row1)
        af_inner.addLayout(af_row1b)
        af_inner.addLayout(af_row2)

        # [Annotation] group
        annotation_group = QtWidgets.QGroupBox(tr("Annotation"))
        annotation_inner = QtWidgets.QHBoxLayout(annotation_group)
        annotation_inner.addWidget(QtWidgets.QLabel(tr("Draw:")))
        annotation_inner.addWidget(self.btn_draw_circle)
        annotation_inner.addWidget(self.btn_draw_rect)
        annotation_inner.addWidget(self.btn_draw_line)
        annotation_inner.addWidget(self.btn_draw_cross)
        annotation_inner.addSpacing(16)
        annotation_inner.addWidget(self.btn_toggle_marks)
        annotation_inner.addWidget(self.btn_toggle_timestamp)
        annotation_inner.addStretch()

        # [Recording] group
        recording_group = QtWidgets.QGroupBox(tr("Recording"))
        recording_inner = QtWidgets.QHBoxLayout(recording_group)
        recording_inner.addWidget(self.btn_snapshot)
        recording_inner.addWidget(self.btn_start_record)
        recording_inner.addWidget(self.btn_stop_record)
        recording_inner.addStretch()

        tab1_widget = QtWidgets.QWidget()
        tab1_layout = QtWidgets.QVBoxLayout(tab1_widget)
        tab1_layout.addWidget(self.video_label, stretch=1)
        tab1_layout.addWidget(self.ctm_notice_label)
        tab1_layout.addWidget(stage_ctrl_group)
        tab1_layout.addWidget(recording_group)
        tab1_layout.addWidget(annotation_group)
        tab1_layout.addWidget(af_group)
        tab1_layout.addWidget(calib_group)
        tab1_layout.addWidget(self.status_label)

        tab2_widget = self._create_tracking_tab()

        self.tab_widget = QtWidgets.QTabWidget()
        self.tab_widget.addTab(tab1_widget, tr("Interactive Camera"))
        self.tab_widget.addTab(tab2_widget, tr("Sample Tracking (Advanced)"))
        self.setCentralWidget(self.tab_widget)

        self._setup_menu_bar()

        self._tracking_log_signal.connect(self._log_tracking_slot)

        self.follow_timer = QtCore.QTimer(self)
        self.follow_timer.timeout.connect(self._follow_iteration)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_frame)
        self.timer.start(30)

        self._shapes_save_timer = QtCore.QTimer(self)
        self._shapes_save_timer.setSingleShot(True)
        self._shapes_save_timer.setInterval(500)
        self._shapes_save_timer.timeout.connect(self._save_shapes)
        self.radius_popup.spinbox.valueChanged.connect(lambda _: self._shapes_save_timer.start())

    # ------------------------------------------------------------------ menu bar

    def _setup_menu_bar(self):
        menu_bar = self.menuBar()
        settings_menu = menu_bar.addMenu(tr("Settings"))
        af_settings_action = settings_menu.addAction(tr("Auto Focus…"))
        af_settings_action.triggered.connect(self._on_open_autofocus_settings)

    # ------------------------------------------------------------------ drawing mode

    def _set_draw_mode(self, mode):
        """Toggle draw mode on/off. Clicking the active mode's button deactivates it."""
        if self.draw_mode == mode:
            self.draw_mode = None
        else:
            self.draw_mode = mode

        # Reset any in-progress draw state
        self.draw_preview = None
        self.line_first_point = None
        self.drag_start_frame = None
        self.selected_idx = -1
        self._update_radius_popup()

        # Sync button checked states
        self.btn_draw_circle.setChecked(self.draw_mode == 'circle')
        self.btn_draw_rect.setChecked(self.draw_mode == 'rect')
        self.btn_draw_line.setChecked(self.draw_mode == 'line')
        self.btn_draw_cross.setChecked(self.draw_mode == 'cross')

        if self.draw_mode:
            # Disable click-to-move while in draw mode
            if self.click_to_move_enabled:
                self.click_to_move_enabled = False
                self.click_to_move_checkbox.setChecked(False)
            self.video_label.setCursor(QtCore.Qt.CursorShape.CrossCursor)
            hint = tr("Drag to draw.") if self.draw_mode != 'line' else tr("Click first point.")
            self.status_label.setText(tr("Draw [{mode}]: {hint}", mode=self.draw_mode, hint=hint))
        else:
            self.video_label.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
            self.status_label.setText(tr("Ready."))

    def _exit_draw_mode(self):
        """Called after a shape is finalized — turns draw mode off."""
        self.draw_mode = None
        self.draw_preview = None
        self.line_first_point = None
        self.drag_start_frame = None
        self.btn_draw_circle.setChecked(False)
        self.btn_draw_rect.setChecked(False)
        self.btn_draw_line.setChecked(False)
        self.btn_draw_cross.setChecked(False)
        self.video_label.setCursor(QtCore.Qt.CursorShape.ArrowCursor)

    # ------------------------------------------------------------------ shape helpers

    def _find_shape_at(self, fx, fy, threshold=10):
        """Return index of the topmost shape under (fx, fy) in frame coords, or -1."""
        for idx in range(len(self.shapes) - 1, -1, -1):
            if self._shape_hit(self.shapes[idx], fx, fy, threshold):
                return idx
        return -1

    def _shape_hit(self, s, fx, fy, threshold=10):
        t = s['type']
        if t == 'circle':
            dist = math.hypot(fx - s['cx'], fy - s['cy'])
            return abs(dist - s['r']) <= threshold or dist <= threshold
        if t == 'rect':
            x1, y1 = min(s['x1'], s['x2']), min(s['y1'], s['y2'])
            x2, y2 = max(s['x1'], s['x2']), max(s['y1'], s['y2'])
            near_v = x1 - threshold <= fx <= x2 + threshold
            near_h = y1 - threshold <= fy <= y2 + threshold
            return (
                (abs(fx - x1) <= threshold and near_h) or
                (abs(fx - x2) <= threshold and near_h) or
                (abs(fy - y1) <= threshold and near_v) or
                (abs(fy - y2) <= threshold and near_v)
            )
        if t == 'line':
            return self._pt_seg_dist(fx, fy, s['x1'], s['y1'], s['x2'], s['y2']) <= threshold
        if t == 'cross':
            if math.hypot(fx - s['cx'], fy - s['cy']) <= threshold:
                return True
            r = s['r']
            on_h = abs(fy - s['cy']) <= threshold and abs(fx - s['cx']) <= r + threshold
            on_v = abs(fx - s['cx']) <= threshold and abs(fy - s['cy']) <= r + threshold
            return on_h or on_v
        return False

    def _pt_seg_dist(self, px, py, x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            return math.hypot(px - x1, py - y1)
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

    def _snap_angle(self, x1, y1, x2, y2):
        """Snap (x2,y2) to the nearest 45° direction from (x1,y1)."""
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            return x2, y2
        angle = math.atan2(dy, dx)
        snapped = round(angle / (math.pi / 4)) * (math.pi / 4)
        dist = math.hypot(dx, dy)
        return int(x1 + dist * math.cos(snapped)), int(y1 + dist * math.sin(snapped))

    def _move_shape(self, idx, ddx, ddy, origin):
        """Translate shape[idx] by (ddx, ddy) relative to its origin snapshot."""
        s, o = self.shapes[idx], origin
        if o['type'] in ('circle', 'cross'):
            s['cx'] = o['cx'] + ddx
            s['cy'] = o['cy'] + ddy
        elif o['type'] in ('rect', 'line'):
            s['x1'] = o['x1'] + ddx
            s['y1'] = o['y1'] + ddy
            s['x2'] = o['x2'] + ddx
            s['y2'] = o['y2'] + ddy

    def _remove_shape(self, idx):
        if 0 <= idx < len(self.shapes):
            self.shapes.pop(idx)
            if self.selected_idx == idx:
                self.selected_idx = -1
            elif self.selected_idx > idx:
                self.selected_idx -= 1
            self.status_label.setText(tr("Removed mark #{n}.", n=idx + 1))
            self._save_shapes()
        self._update_radius_popup()

    def _update_radius_popup(self):
        """Show the radius popup near the selected circle, or hide it otherwise."""
        idx = self.selected_idx
        if (idx >= 0 and idx < len(self.shapes)
                and self.shapes[idx].get('type') in ('circle', 'cross')):
            s = self.shapes[idx]
            scale = self.render_params.get('scale', 1.0)
            dx = self.render_params.get('dx', 0)
            dy = self.render_params.get('dy', 0)
            lx = int(s['cx'] * scale + dx)
            ly = int(s['cy'] * scale + dy)
            global_pt = self.video_label.mapToGlobal(QtCore.QPoint(lx, ly))
            pw = self.radius_popup.width()
            self.radius_popup.attach(self.shapes, idx)
            self.radius_popup.move(global_pt.x() - pw // 2, global_pt.y() + 18)
            self.radius_popup.show()
        else:
            self.radius_popup.detach()
            self.radius_popup.hide()

    # ------------------------------------------------------------------ mouse events

    def on_video_wheel(self, direction):
        """Adjust the radius of the selected circle/cross by scroll wheel."""
        idx = self.selected_idx
        if idx < 0 or idx >= len(self.shapes):
            return
        s = self.shapes[idx]
        if s.get('type') not in ('circle', 'cross'):
            return
        mods = QtWidgets.QApplication.keyboardModifiers()
        step = 10 if bool(mods & QtCore.Qt.KeyboardModifier.ShiftModifier) else 1
        s['r'] = max(1, s['r'] + direction * step)
        self._update_radius_popup()

    def on_video_left_click(self, point):
        if self.current_frame is None:
            return

        fx, fy = self.label_to_frame_coords(point)
        if fx < 0 or fy < 0:
            return

        # Calibration pixel capture takes priority
        if self.calibration_dialog and self.calibration_dialog.waiting_for_pixel_click:
            self.calibration_dialog.record_pixel_position(fx, fy)
            self.status_label.setText(tr("Calibration pixel recorded at ({x}, {y}).", x=fx, y=fy))
            return

        # Click-to-move mode
        if self.click_to_move_enabled and not self.calibration_mode:
            if self.calibration_data and not self.is_moving:
                cx = self.frame_width / 2.0
                cy = self.frame_height / 2.0
                M_inv = np.array(self.calibration_data['matrix_inv'])
                motor_disp = M_inv @ np.array([cx - fx, cy - fy])
                diff_ch4 = int(motor_disp[0])
                diff_ch5 = int(motor_disp[1])
                self.is_moving = True
                self.status_label.setText(
                    tr("Centering... Moving Ch4={ch4:+}, Ch5={ch5:+}", ch4=diff_ch4, ch5=diff_ch5))

                _ctm_speed = ("H" if self.ctm_speed_h.isChecked()
                              else "L" if self.ctm_speed_l.isChecked() else "M")

                def move_task():
                    try:
                        with self.controller.motion_session(
                            owner="Interactive Camera",
                            operation="Click-to-move centring",
                        ) as motion:
                            self.controller.set_ch_speed(4, _ctm_speed, motion=motion)
                            self.controller.set_ch_speed(5, _ctm_speed, motion=motion)
                            self.controller.switch_to_rem(motion=motion)
                            self.controller.move_ch_relative(4, diff_ch4, motion=motion)
                            self.controller.move_ch_relative(5, diff_ch5, motion=motion)
                            self.controller.wait_until_stop(stay_in_rem=True)
                            # self.controller.switch_to_loc()
                        QtCore.QMetaObject.invokeMethod(
                            self.status_label, "setText",
                            QtCore.Qt.ConnectionType.QueuedConnection,
                            QtCore.Q_ARG(str, tr("Movement complete. Ready.")))
                    except Exception as exc:
                        print(f"Error moving stage: {exc}")
                        QtCore.QMetaObject.invokeMethod(
                            self.status_label, "setText",
                            QtCore.Qt.ConnectionType.QueuedConnection,
                            QtCore.Q_ARG(str, tr("Error moving stage.")))
                    finally:
                        self.is_moving = False

                self._move_thread = threading.Thread(target=move_task, daemon=True)
                self._move_thread.start()
            return

        # --- Draw mode: circle, rect, or cross (drag) ---
        if self.draw_mode in ('circle', 'rect', 'cross'):
            self.drag_start_frame = (fx, fy)
            self.draw_preview = {'type': self.draw_mode, 'x1': fx, 'y1': fy, 'x2': fx, 'y2': fy}
            return

        # --- Draw mode: line (two-click) ---
        if self.draw_mode == 'line':
            if self.line_first_point is None:
                self.line_first_point = (fx, fy)
                self.draw_preview = {'type': 'line', 'x1': fx, 'y1': fy, 'x2': fx, 'y2': fy}
                self.status_label.setText(tr("Line: click second point (hold Shift to constrain angle)."))
            else:
                x1, y1 = self.line_first_point
                mods = QtWidgets.QApplication.keyboardModifiers()
                if mods & QtCore.Qt.KeyboardModifier.ShiftModifier:
                    fx, fy = self._snap_angle(x1, y1, fx, fy)
                self.shapes.append({'type': 'line', 'x1': x1, 'y1': y1, 'x2': fx, 'y2': fy, 'thickness': 1.5})
                self.selected_idx = len(self.shapes) - 1
                self._exit_draw_mode()
                self._update_radius_popup()
                self.status_label.setText(tr("Line added."))
                self._save_shapes()
            return

        # --- Selection / drag mode ---
        hit_idx = self._find_shape_at(fx, fy)
        if hit_idx >= 0:
            self.selected_idx = hit_idx
            self.is_dragging_shape = True
            self.drag_start_frame = (fx, fy)
            self.drag_shape_origin = dict(self.shapes[hit_idx])
            self._update_radius_popup()
            self.status_label.setText(
                tr("Selected {type} #{n}. Drag to move.",
                   type=self.shapes[hit_idx]['type'], n=hit_idx + 1))
        else:
            self.selected_idx = -1
            self._update_radius_popup()

    def on_video_mouse_move(self, point):
        if self.current_frame is None:
            return

        fx, fy = self.label_to_frame_coords(point)

        mods = QtWidgets.QApplication.keyboardModifiers()
        shift = bool(mods & QtCore.Qt.KeyboardModifier.ShiftModifier)

        # Update draw preview
        if self.draw_mode in ('circle', 'rect', 'cross') and self.drag_start_frame and fx >= 0:
            x1, y1 = self.drag_start_frame
            ex, ey = fx, fy
            if self.draw_mode == 'rect' and shift:
                side = max(abs(ex - x1), abs(ey - y1))
                ex = x1 + (side if ex >= x1 else -side)
                ey = y1 + (side if ey >= y1 else -side)
            self.draw_preview = {
                'type': self.draw_mode,
                'x1': x1,
                'y1': y1,
                'x2': ex,
                'y2': ey,
            }
        elif self.draw_mode == 'line' and self.line_first_point and fx >= 0:
            x1, y1 = self.line_first_point
            ex, ey = (self._snap_angle(x1, y1, fx, fy) if shift else (fx, fy))
            self.draw_preview = {'type': 'line', 'x1': x1, 'y1': y1, 'x2': ex, 'y2': ey}

        # Drag selected shape
        elif self.is_dragging_shape and self.selected_idx >= 0 and self.drag_start_frame and fx >= 0:
            ddx = fx - self.drag_start_frame[0]
            ddy = fy - self.drag_start_frame[1]
            self._move_shape(self.selected_idx, ddx, ddy, self.drag_shape_origin)

    def on_video_left_release(self, point):
        # Finalize circle, rect, or cross drawn by drag
        if self.draw_mode in ('circle', 'rect', 'cross') and self.drag_start_frame:
            fx, fy = self.label_to_frame_coords(point)
            x1, y1 = self.drag_start_frame
            # Clamp to frame if out of bounds
            x2 = max(0, min(self.frame_width - 1, fx)) if fx >= 0 else x1
            y2 = max(0, min(self.frame_height - 1, fy)) if fy >= 0 else y1

            mods = QtWidgets.QApplication.keyboardModifiers()
            if self.draw_mode == 'rect' and (mods & QtCore.Qt.KeyboardModifier.ShiftModifier):
                side = max(abs(x2 - x1), abs(y2 - y1))
                x2 = x1 + (side if x2 >= x1 else -side)
                y2 = y1 + (side if y2 >= y1 else -side)

            if self.draw_mode == 'circle':
                r = max(1, int(math.hypot(x2 - x1, y2 - y1)))
                self.shapes.append({'type': 'circle', 'cx': x1, 'cy': y1, 'r': r, 'thickness': 1.5})
            elif self.draw_mode == 'cross':
                r = max(1, int(math.hypot(x2 - x1, y2 - y1)))
                self.shapes.append({'type': 'cross', 'cx': x1, 'cy': y1, 'r': r, 'thickness': 1.5})
            else:
                self.shapes.append({'type': 'rect', 'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2, 'thickness': 1.5})

            self.selected_idx = len(self.shapes) - 1
            self._exit_draw_mode()
            self._update_radius_popup()
            self.status_label.setText(tr("{type} added.", type=self.shapes[-1]['type'].capitalize()))
            self._save_shapes()

        # End shape drag
        elif self.is_dragging_shape:
            self.is_dragging_shape = False
            self.drag_start_frame = None
            self.drag_shape_origin = None
            self._save_shapes()

    def on_video_right_click(self, point):
        fx, fy = self.label_to_frame_coords(point)
        menu = QtWidgets.QMenu(self)

        # Detect hit shape up front so all sections can reference it
        hit_idx = -1
        _circle_centre = None
        if fx >= 0 and fy >= 0:
            hit_idx = self._find_shape_at(fx, fy)
            if hit_idx >= 0 and self.shapes[hit_idx]['type'] == 'circle':
                _circle_centre = (self.shapes[hit_idx]['cx'], self.shapes[hit_idx]['cy'])

        # --- Focus ---
        self._add_menu_section(menu, tr("Normal auto focus (Ch3)"), first=True)
        menu.addAction(tr("Execute Auto Focus"), self.start_autofocus)
        if hit_idx >= 0 and self.shapes[hit_idx]['type'] == 'circle':
            af_roi_act = menu.addAction(tr("Execute Auto-Focus inside this circle"))
            af_roi_act.triggered.connect(
                lambda _, i=hit_idx: self._start_autofocus_with_roi(i))

        # --- Focus by Ch7 (hidden feature, right-click only) ---
        self._add_menu_section(menu, tr("Auto Focus by Ch7"))
        menu.addAction(tr("Execute Auto-focus by scanning Ch7"), self.start_autofocus_ch7)
        if hit_idx >= 0 and self.shapes[hit_idx]['type'] == 'circle':
            af7_roi_act = menu.addAction(tr("Execute Auto-focus inside this circle by scanning Ch7"))
            af7_roi_act.triggered.connect(
                lambda _, i=hit_idx: self._start_autofocus_with_roi_ch7(i))

        # --- Mark (only when a shape is hit) ---
        if hit_idx >= 0:
            shape_name = self.shapes[hit_idx]['type'].capitalize()
            self._add_menu_section(menu, tr("Mark"))
            remove_act = menu.addAction(tr("Remove this mark ({name} #{n})", name=shape_name, n=hit_idx + 1))
            remove_act.triggered.connect(lambda: self._remove_shape(hit_idx))
            thickness_menu = menu.addMenu(tr("Line Thickness"))
            current_thickness = self.shapes[hit_idx].get('thickness', 2)
            for label, value in [("Thin", 1), ("Regular", 1.5), ("Bold", 3)]:
                act = thickness_menu.addAction(tr(label))
                act.setCheckable(True)
                act.setChecked(current_thickness == value)
                act.triggered.connect(
                    lambda _, v=value, i=hit_idx: self._set_shape_thickness(i, v))

        # --- Stage Move (only when calibration is available) ---
        if self.calibration_data is not None and fx >= 0 and fy >= 0:
            self._add_menu_section(menu, tr("Move this position to:"))
            _bold_font = QtGui.QFont()
            _bold_font.setBold(True)
            if self.laser_spot_pos is not None:
                move_laser_act = menu.addAction(tr("Laser spot"))
                move_laser_act.setFont(_bold_font)
                move_laser_act.triggered.connect(lambda: self._move_to_laser_spot(fx, fy))
            if self.xray_beam_pos is not None:
                move_xray_act = menu.addAction(tr("X-ray beam"))
                move_xray_act.setFont(_bold_font)
                move_xray_act.triggered.connect(lambda: self._move_to_xray_beam_pos(fx, fy))
            centre_act = menu.addAction(tr("Centre of the image"))
            centre_act.setFont(_bold_font)
            centre_act.triggered.connect(lambda: self._move_to_centre(fx, fy))

        # --- Reference Points ---
        self._add_menu_section(menu, tr("Remember this position as:"))
        remember_act = menu.addAction(tr("Laser spot position"))
        remember_act.triggered.connect(lambda: self._set_laser_spot(fx, fy))
        remember_xray_act = menu.addAction(tr("X-ray beam position"))
        remember_xray_act.triggered.connect(lambda: self._set_xray_beam_pos(fx, fy))
        if _circle_centre is not None:
            remember_circle_xray_act = menu.addAction(
                tr("Remember the centre of this circle as the x-ray beam position"))
            remember_circle_xray_act.triggered.connect(
                lambda _, cx=_circle_centre[0], cy=_circle_centre[1]: self._set_xray_beam_pos(cx, cy))

        menu.exec(self.video_label.mapToGlobal(point))

    @staticmethod
    def _add_menu_section(menu, title, first=False):
        if not first:
            menu.addSeparator()
        action = menu.addAction(title)
        action.setEnabled(False)
        font = action.font()
        font.setBold(True)
        action.setFont(font)

    def _set_shape_thickness(self, idx, thickness):
        if 0 <= idx < len(self.shapes):
            self.shapes[idx]['thickness'] = thickness
            self._save_shapes()

    # ------------------------------------------------------------------ calibration

    def open_calibration_dialog(self):
        # The lease spans the whole dialog session (opened here, released in
        # on_calibration_closed) since the user drives moves interactively
        # while it's open — not a single move+wait sequence.
        try:
            self._calibration_lease = self.controller.acquire_motion(
                owner="Interactive Camera", operation="Calibration",
            )
            self.controller.switch_to_rem(motion=self._calibration_lease)
            if self.calibration_dialog is None:
                self.calibration_dialog = CalibrationDialog(
                    self.controller, self, motion=self._calibration_lease,
                )
                self.calibration_dialog.finished.connect(self.on_calibration_closed)
                self.calibration_mode = True
                self.calibration_dialog.show()
                self.status_label.setText(tr("Calibration dialog opened. Record motor positions and click on the video image."))
        except Exception as exc:
            self._calibration_lease = None
            QtWidgets.QMessageBox.critical(self, tr("Error"), tr("Could not switch to remote mode: {error}", error=exc))

    def on_calibration_closed(self, result):
        lease = getattr(self, "_calibration_lease", None)
        try:
            if lease is not None and self.controller.coordinator.is_valid(lease):
                self.controller.switch_to_loc(motion=lease)
        except Exception:
            pass
        if lease is not None:
            self.controller.release_motion(lease)
            self._calibration_lease = None
        if self.calibration_dialog and self.calibration_dialog.get_calibration():
            self.calibration_data = self.calibration_dialog.get_calibration()
            self.click_to_move_checkbox.setEnabled(True)
            self.calibration_mode = False
            try:
                with open(self.calibration_filepath, 'w') as f:
                    json.dump(self.calibration_data, f, indent=4)
                print("Saved calibration data to JSON.")
            except Exception as e:
                print(f"Failed to save calibration data: {e}")
            QtWidgets.QMessageBox.information(self, tr("Calibration"), tr("Calibration completed. Click-to-move is now available."))
        else:
            self.status_label.setText(tr("Calibration cancelled or incomplete."))
            self.calibration_mode = False
        self.calibration_dialog = None

    def on_click_to_move_toggled(self, checked):
        self.click_to_move_enabled = checked
        self.ctm_notice_label.setVisible(checked)

    # ------------------------------------------------------------------ centre move

    def _move_to_centre(self, fx, fy):
        if self.calibration_data is None or self.is_moving:
            return
        cx = self.frame_width / 2.0
        cy = self.frame_height / 2.0
        M_inv = np.array(self.calibration_data['matrix_inv'])
        motor_disp = M_inv @ np.array([cx - fx, cy - fy])
        diff_ch4 = int(motor_disp[0])
        diff_ch5 = int(motor_disp[1])
        self.is_moving = True
        _spd = "H" if self.ctm_speed_h.isChecked() else "L" if self.ctm_speed_l.isChecked() else "M"
        self.status_label.setText(tr("Centring... Ch4={ch4:+}, Ch5={ch5:+}", ch4=diff_ch4, ch5=diff_ch5))

        def task():
            try:
                with self.controller.motion_session(
                    owner="Interactive Camera", operation="Centre move",
                ) as motion:
                    self.controller.set_ch_speed(4, _spd, motion=motion)
                    self.controller.set_ch_speed(5, _spd, motion=motion)
                    self.controller.switch_to_rem(motion=motion)
                    self.controller.move_ch_relative(4, diff_ch4, motion=motion)
                    self.controller.move_ch_relative(5, diff_ch5, motion=motion)
                    self.controller.wait_until_stop(motion=motion)
                QtCore.QMetaObject.invokeMethod(
                    self.status_label, "setText",
                    QtCore.Qt.ConnectionType.QueuedConnection,
                    QtCore.Q_ARG(str, tr("Centring complete.")))
            except Exception as exc:
                print(f"Error centring: {exc}")
                QtCore.QMetaObject.invokeMethod(
                    self.status_label, "setText",
                    QtCore.Qt.ConnectionType.QueuedConnection,
                    QtCore.Q_ARG(str, tr("Error centring: {error}", error=exc)))
            finally:
                self.is_moving = False

        self._move_thread = threading.Thread(target=task, daemon=True)
        self._move_thread.start()

    # ------------------------------------------------------------------ laser spot

    def _load_laser_spot(self):
        try:
            with open(self._laser_spot_path, 'r') as f:
                data = json.load(f)
            self.laser_spot_pos = (int(data['x']), int(data['y']))
            print(f"Loaded laser spot position: {self.laser_spot_pos}")
        except Exception:
            self.laser_spot_pos = None

    def _save_laser_spot(self):
        os.makedirs(self._localdata_dir, exist_ok=True)
        with open(self._laser_spot_path, 'w') as f:
            json.dump({'x': self.laser_spot_pos[0], 'y': self.laser_spot_pos[1]}, f)

    # ------------------------------------------------------------------ shapes persistence

    def _load_shapes(self):
        try:
            with open(self._shapes_path, 'r') as f:
                data = json.load(f)
            self.shapes = data.get('shapes', [])
            print(f"Loaded {len(self.shapes)} shape(s) from shapes.json.")
        except Exception:
            self.shapes = []

    def _save_shapes(self):
        try:
            os.makedirs(self._localdata_dir, exist_ok=True)
            with open(self._shapes_path, 'w') as f:
                json.dump({'shapes': self.shapes}, f, indent=2)
        except Exception as e:
            print(f"Warning: could not save shapes: {e}")

    def _set_laser_spot(self, fx, fy):
        self.laser_spot_pos = (int(round(fx)), int(round(fy)))
        self._save_laser_spot()
        self.laser_spot_label.setText(
            tr("Laser position: ({x}, {y})", x=self.laser_spot_pos[0], y=self.laser_spot_pos[1]))
        self.status_label.setText(
            tr("Laser spot set to pixel ({x}, {y}).", x=self.laser_spot_pos[0], y=self.laser_spot_pos[1]))

    def _move_to_laser_spot(self, fx, fy):
        if self.laser_spot_pos is None or self.calibration_data is None or self.is_moving:
            return
        laser_fx, laser_fy = self.laser_spot_pos
        M_inv = np.array(self.calibration_data['matrix_inv'])
        motor_disp = M_inv @ np.array([laser_fx - fx, laser_fy - fy])
        diff_ch4 = int(motor_disp[0])
        diff_ch5 = int(motor_disp[1])
        self.is_moving = True
        self.status_label.setText(
            tr("Moving to laser spot... Ch4={ch4:+}, Ch5={ch5:+}", ch4=diff_ch4, ch5=diff_ch5))
        _spd = "H" if self.ctm_speed_h.isChecked() else "L" if self.ctm_speed_l.isChecked() else "M"

        def task():
            try:
                with self.controller.motion_session(
                    owner="Interactive Camera", operation="Move to laser spot",
                ) as motion:
                    self.controller.set_ch_speed(4, _spd, motion=motion)
                    self.controller.set_ch_speed(5, _spd, motion=motion)
                    self.controller.switch_to_rem(motion=motion)
                    self.controller.move_ch_relative(4, diff_ch4, motion=motion)
                    self.controller.move_ch_relative(5, diff_ch5, motion=motion)
                    self.controller.wait_until_stop(motion=motion)
                QtCore.QMetaObject.invokeMethod(
                    self.status_label, "setText",
                    QtCore.Qt.ConnectionType.QueuedConnection,
                    QtCore.Q_ARG(str, tr("Moved to laser spot position.")))
            except Exception as exc:
                print(f"Error moving to laser spot: {exc}")
                QtCore.QMetaObject.invokeMethod(
                    self.status_label, "setText",
                    QtCore.Qt.ConnectionType.QueuedConnection,
                    QtCore.Q_ARG(str, tr("Error moving to laser spot: {error}", error=exc)))
            finally:
                self.is_moving = False

        self._move_thread = threading.Thread(target=task, daemon=True)
        self._move_thread.start()

    # ------------------------------------------------------------------ x-ray beam position

    def _set_xray_beam_pos(self, fx, fy):
        self.xray_beam_pos = (int(round(fx)), int(round(fy)))
        self.xray_beam_label.setText(
            tr("x-ray beam position: ({x}, {y})", x=self.xray_beam_pos[0], y=self.xray_beam_pos[1]))
        self.status_label.setText(
            tr("x-ray beam position set to pixel ({x}, {y}).", x=self.xray_beam_pos[0], y=self.xray_beam_pos[1]))

    def _move_to_xray_beam_pos(self, fx, fy):
        if self.xray_beam_pos is None or self.calibration_data is None or self.is_moving:
            return
        xray_fx, xray_fy = self.xray_beam_pos
        M_inv = np.array(self.calibration_data['matrix_inv'])
        motor_disp = M_inv @ np.array([xray_fx - fx, xray_fy - fy])
        diff_ch4 = int(motor_disp[0])
        diff_ch5 = int(motor_disp[1])
        self.is_moving = True
        self.status_label.setText(
            tr("Moving to x-ray beam position... Ch4={ch4:+}, Ch5={ch5:+}", ch4=diff_ch4, ch5=diff_ch5))
        _spd = "H" if self.ctm_speed_h.isChecked() else "L" if self.ctm_speed_l.isChecked() else "M"

        def task():
            try:
                with self.controller.motion_session(
                    owner="Interactive Camera", operation="Move to X-ray beam position",
                ) as motion:
                    self.controller.set_ch_speed(4, _spd, motion=motion)
                    self.controller.set_ch_speed(5, _spd, motion=motion)
                    self.controller.switch_to_rem(motion=motion)
                    self.controller.move_ch_relative(4, diff_ch4, motion=motion)
                    self.controller.move_ch_relative(5, diff_ch5, motion=motion)
                    self.controller.wait_until_stop(motion=motion)
                QtCore.QMetaObject.invokeMethod(
                    self.status_label, "setText",
                    QtCore.Qt.ConnectionType.QueuedConnection,
                    QtCore.Q_ARG(str, tr("Moved to x-ray beam position.")))
            except Exception as exc:
                print(f"Error moving to x-ray beam position: {exc}")
                QtCore.QMetaObject.invokeMethod(
                    self.status_label, "setText",
                    QtCore.Qt.ConnectionType.QueuedConnection,
                    QtCore.Q_ARG(str, tr("Error moving to x-ray beam position: {error}", error=exc)))
            finally:
                self.is_moving = False

        self._move_thread = threading.Thread(target=task, daemon=True)
        self._move_thread.start()

    # ------------------------------------------------------------------ stage relative move

    def _move_relative_ch(self, ch, diff):
        if self.is_moving:
            self.status_label.setText(tr("Move in progress, please wait."))
            return
        self.is_moving = True
        self.status_label.setText(tr("Moving Ch{ch} {diff:+} pulses...", ch=ch, diff=diff))

        def task():
            try:
                with self.controller.motion_session(
                    owner="Interactive Camera", operation=f"Move Ch{ch}",
                ) as motion:
                    self.controller.move_ch_relative(ch, diff, motion=motion)
                    self.controller.wait_until_stop(motion=motion)
                QtCore.QMetaObject.invokeMethod(
                    self.status_label, "setText",
                    QtCore.Qt.ConnectionType.QueuedConnection,
                    QtCore.Q_ARG(str, tr("Ch{ch} moved {diff:+} pulses. Ready.", ch=ch, diff=diff)))
            except Exception as exc:
                print(f"Relative move Ch{ch} error: {exc}")
                QtCore.QMetaObject.invokeMethod(
                    self.status_label, "setText",
                    QtCore.Qt.ConnectionType.QueuedConnection,
                    QtCore.Q_ARG(str, tr("Error: {msg}", msg=exc)))
            finally:
                self.is_moving = False

        self._move_thread = threading.Thread(target=task, daemon=True)
        self._move_thread.start()
        _state = tr("enabled") if self.click_to_move_enabled else tr("disabled")
        self.status_label.setText(tr("Click-to-move {state}.", state=_state))

    # ------------------------------------------------------------------ autofocus

    def _start_autofocus_with_roi(self, shape_idx):
        if shape_idx < 0 or shape_idx >= len(self.shapes):
            return
        s = self.shapes[shape_idx]
        if s.get('type') != 'circle':
            return
        self.autofocus.roi = {'cx': s['cx'], 'cy': s['cy'], 'r': s['r']}
        self.status_label.setText(
            tr("Auto-Focus with ROI: circle center=({cx}, {cy}), r={r}", cx=s['cx'], cy=s['cy'], r=s['r']))
        self.start_autofocus()

    def start_autofocus(self):
        if self.is_moving:
            QtWidgets.QMessageBox.warning(self, tr("Warning"), tr("Stage is currently moving. Please wait."))
            return
        if self.autofocus.is_autofocusing():
            QtWidgets.QMessageBox.information(self, tr("Auto Focus"), tr("Auto-focus is already running."))
            return
        self.autofocus.completion_callback = (
            self._on_autofocus_complete if log_prefs.should_save("autofocus") else None
        )
        um = self.focus_range_spinbox.value()
        self.autofocus.focus_range = um // 2
        speed = "H" if self.focus_speed_h.isChecked() else ("M" if self.focus_speed_m.isChecked() else "L")
        try:
            motion = self.controller.acquire_motion(
                owner="Interactive Camera", operation=f"Autofocus Ch{self.autofocus.channel}",
            )
            self.controller.set_ch_speed(3, speed, motion=motion)
            self.controller.switch_to_rem(motion=motion)
            if self.autofocus.perform_autofocus(motion=motion):
                self.status_label.setText(tr("Auto-focus started... (scan range: ±{um} um)", um=um))
            else:
                self.controller.switch_to_loc(motion=motion)
                self.controller.release_motion(motion)
                QtWidgets.QMessageBox.critical(self, tr("Error"), tr("Failed to start auto-focus."))
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, tr("Error"), tr("Failed to start auto-focus: {error}", error=exc))

    def _start_autofocus_with_roi_ch7(self, shape_idx):
        if shape_idx < 0 or shape_idx >= len(self.shapes):
            return
        s = self.shapes[shape_idx]
        if s.get('type') != 'circle':
            return
        self.autofocus_ch7.roi = {'cx': s['cx'], 'cy': s['cy'], 'r': s['r']}
        self.status_label.setText(
            tr("Auto-Focus (Ch7) with ROI: circle center=({cx}, {cy}), r={r}", cx=s['cx'], cy=s['cy'], r=s['r']))
        self.start_autofocus_ch7()

    def start_autofocus_ch7(self):
        _CH7_RANGE_UM = 30    # fixed half-range in µm
        _CH7_STEP_UM  = 5     # fixed step in µm
        if self.is_moving:
            QtWidgets.QMessageBox.warning(self, tr("Warning"), tr("Stage is currently moving. Please wait."))
            return
        if self.autofocus.is_autofocusing() or self.autofocus_ch7.is_autofocusing():
            QtWidgets.QMessageBox.information(self, tr("Auto Focus"), tr("Auto-focus is already running."))
            return
        reply = QtWidgets.QMessageBox.question(
            self, tr("Auto-Focus by Ch7"),
            tr("Scan Ch7 by ±{range} µm. Are you sure?", range=_CH7_RANGE_UM),
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Ok:
            return
        self.autofocus_ch7.focus_range = int(_CH7_RANGE_UM / UM_PER_PULSE_CH7)
        self.autofocus_ch7.step_size   = int(_CH7_STEP_UM  / UM_PER_PULSE_CH7)
        self.autofocus_ch7.method      = self.autofocus.method
        self.autofocus_ch7.n_frames    = self.autofocus.n_frames
        self.autofocus_ch7.peak_method = self.autofocus.peak_method
        try:
            motion = self.controller.acquire_motion(
                owner="Interactive Camera", operation="Autofocus Ch7",
            )
            self.controller.set_ch_speed(7, "H", motion=motion)
            self.controller.switch_to_rem(motion=motion)
            if self.autofocus_ch7.perform_autofocus(motion=motion):
                self.status_label.setText(
                    tr("Auto-focus (Ch7) started... (±{range} µm, step {step} µm)",
                       range=_CH7_RANGE_UM, step=_CH7_STEP_UM))
            else:
                self.controller.switch_to_loc(motion=motion)
                self.controller.release_motion(motion)
                QtWidgets.QMessageBox.critical(self, tr("Error"), tr("Failed to start auto-focus (Ch7)."))
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, tr("Error"), tr("Failed to start auto-focus (Ch7): {error}", error=exc))

    def stop_autofocus(self):
        # Async stop: request_emergency_stop() never blocks the GUI thread.
        # revoke_for_stop() inside it invalidates the autofocus lease
        # immediately; the autofocus worker thread's finally releases it.
        if self.autofocus.stop_autofocus():
            self.status_label.setText(tr("Auto-focus stopped."))
            if self.controller is not None:
                self.controller.request_emergency_stop(source="Interactive Camera")
        else:
            self.status_label.setText(tr("No auto-focus operation was running."))

    # --------------------------------------------------------- AF settings sync

    def _af_sync_to_tracking(self):
        """Push AF settings from camera-tab controls → tracking-tab controls (one-way)."""
        if self._af_syncing or not hasattr(self, 'tr_focus_range_spinbox'):
            return
        self._af_syncing = True
        try:
            self.tr_focus_range_spinbox.setValue(self.focus_range_spinbox.value())
            self.tr_focus_step_spinbox.setValue(self.focus_step_spinbox.value())
            self.tr_focus_nframes_spinbox.setValue(self.focus_nframes_spinbox.value())
            self.tr_focus_speed_group.button(self.focus_speed_group.checkedId()).setChecked(True)
        finally:
            self._af_syncing = False

    # Camera-tab AF control callbacks (push to autofocus + sync to tracking tab)
    def _on_focus_range_changed(self, value):
        self.autofocus.focus_range = value // 2
        self._af_sync_to_tracking()

    def _on_focus_step_changed(self, value):
        self.autofocus.step_size = value
        self._af_sync_to_tracking()

    def _on_focus_nframes_changed(self, value):
        self.autofocus.n_frames = value
        self._af_sync_to_tracking()

    def _on_focus_speed_changed(self):
        self._af_sync_to_tracking()

    # Tracking-tab AF control callbacks (push to autofocus only, no reverse sync)
    def _on_tr_focus_range_changed(self, value):
        self.autofocus.focus_range = value // 2

    def _on_tr_focus_step_changed(self, value):
        self.autofocus.step_size = value

    def _on_tr_focus_nframes_changed(self, value):
        self.autofocus.n_frames = value

    def _on_tr_focus_speed_changed(self):
        pass  # speed is applied at scan start

    def _on_open_autofocus_settings(self):
        dlg = AutoFocusSettingsDialog(self.autofocus.method, self.autofocus.peak_method, self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.autofocus.method = dlg.method
            self.autofocus.peak_method = dlg.peak_method

    def _on_autofocus_complete(self, sharpness_data, best_pos, best_sharpness, fit_result=None):
        """Callback when autofocus completes — saves CSV (and optional fit plot) in details mode."""
        if not sharpness_data:
            return

        try:
            af_dir = str(log_prefs.get_app_dir("autofocus"))
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = f"autofocus_{timestamp}"

            method_label = 'Tenengrad' if self.autofocus.method == 'tenengrad' else 'Laplacian'
            step_pulse = self.autofocus.step_size
            step_um = step_pulse * UM_PER_PULSE_CH3
            n_frames = self.autofocus.n_frames
            peak_label = 'Gaussian fit' if self.autofocus.peak_method == 'gaussian' else 'Highest sharpness'

            # --- CSV ---
            csv_path = os.path.join(af_dir, f"{stem}.csv")
            with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
                csvfile.write(f'# Sharpness Metric: {method_label}\n')
                csvfile.write(f'# Step Size: {step_pulse} pulse ({step_um:.1f} um)\n')
                csvfile.write(f'# Frames Averaged per Position: {n_frames}\n')
                csvfile.write(f'# Peak Finding Method: {peak_label}\n')
                if fit_result and fit_result.get('success') and fit_result.get('method') == 'gaussian':
                    csvfile.write(f'# Gaussian Fit: mu={fit_result["mu"]:.3f} pulse, '
                                  f'sigma={abs(fit_result["sigma"]):.3f} pulse, '
                                  f'R2={fit_result["r2"]:.4f}\n')
                writer = csv.writer(csvfile)
                writer.writerow(['Ch3_Position_pulses', 'Ch3_Position_um', 'Sharpness'])
                for pos, sharpness in sharpness_data:
                    writer.writerow([pos, f'{pos * UM_PER_PULSE_CH3:.2f}', f'{sharpness:.6f}'])
            print(f"Autofocus data saved: {csv_path}")

            # --- Gaussian fit plot (details + gaussian + fit succeeded) ---
            if (fit_result and fit_result.get('method') == 'gaussian'
                    and fit_result.get('positions') is not None):
                self._save_autofocus_fit_plot(af_dir, stem, best_pos, fit_result, method_label)

            QtCore.QMetaObject.invokeMethod(
                self.status_label, "setText",
                QtCore.Qt.ConnectionType.QueuedConnection,
                QtCore.Q_ARG(str, tr("Autofocus data saved: {name}.csv", name=stem)))
        except Exception as e:
            import traceback
            print(f"Error saving autofocus data: {e}")
            traceback.print_exc()

    def _save_autofocus_fit_plot(self, af_dir, stem, best_pos, fit_result, method_label):
        try:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_agg import FigureCanvasAgg
        except ImportError:
            print("Warning: matplotlib not installed — skipping fit plot.")
            return

        try:
            from .autofocus import _gaussian as _af_gaussian
        except ImportError:
            from apps.interactive_camera.autofocus import _gaussian as _af_gaussian

        try:
            positions = fit_result['positions']
            sharpnesses = fit_result['sharpnesses']

            fig = Figure(figsize=(8, 5))
            FigureCanvasAgg(fig)
            ax = fig.add_subplot(111)

            ax.scatter(positions, sharpnesses, color='steelblue', s=30, zorder=3,
                       label='Measured sharpness')

            if fit_result.get('success'):
                xs = np.linspace(positions[0], positions[-1], 400)
                ys = _af_gaussian(xs, fit_result['a'], fit_result['mu'],
                                  fit_result['sigma'], fit_result['offset'])
                ax.plot(xs, ys, color='tomato', linewidth=1.5,
                        label=f'Gaussian fit  mu={fit_result["mu"]:.1f} pulse\n'
                              f'sigma={abs(fit_result["sigma"]):.1f} pulse  R2={fit_result["r2"]:.4f}')
                ax.axvline(fit_result['mu'], color='tomato', linestyle='--', linewidth=1, alpha=0.7)
                ax.set_title('Autofocus Sharpness & Gaussian Fit')
            else:
                ax.axvline(best_pos, color='gray', linestyle='--', linewidth=1,
                           label=f'Fallback best pos: {best_pos}')
                ax.set_title(f'Autofocus - Gaussian fit FAILED\n({fit_result.get("error", "")})')

            ax.axvline(best_pos, color='black', linestyle=':', linewidth=1.2, alpha=0.6,
                       label=f'Applied position: {best_pos} pulse')
            ax.set_xlabel('Ch3 Position (pulse)')
            ax.set_ylabel(f'Sharpness ({method_label})')
            ax.legend(fontsize=8)
            ax.grid(True, alpha=0.4)
            fig.tight_layout()

            print("Saving autofocus fit plot...")

            plot_path = os.path.join(af_dir, f"{stem}_fit.png")
            fig.savefig(plot_path, dpi=150)
            print(f"Fit plot saved: {plot_path}")
        except Exception as e:
            import traceback
            print(f"Warning: could not save fit plot: {e}")
            traceback.print_exc()

    def toggle_marks(self):
        self.show_all_marks = not self.show_all_marks
        self.status_label.setText(tr("Show all marks set to {value}.", value=self.show_all_marks))

    def toggle_timestamp(self):
        self.show_timestamp = not self.show_timestamp
        _state = tr("shown") if self.show_timestamp else tr("hidden")
        self.status_label.setText(tr("Timestamp {state}.", state=_state))

    # ------------------------------------------------------------------ localdata cache

    def _load_localdata_cache(self):
        try:
            with open(self._cache_path, 'r') as f:
                cache = json.load(f)
            if 'last_save_dir' in cache and os.path.isdir(cache['last_save_dir']):
                self.last_save_dir = cache['last_save_dir']
            if 'tracking_log_dir' in cache and os.path.isdir(cache['tracking_log_dir']):
                self._cached_tracking_log_dir = cache['tracking_log_dir']
        except Exception:
            pass

    def _save_localdata_cache(self):
        try:
            os.makedirs(self._localdata_dir, exist_ok=True)
            log_dir = ''
            if hasattr(self, 'tracking_log_path_edit'):
                log_dir = self.tracking_log_path_edit.text().strip()
            cache = {
                'last_save_dir': self.last_save_dir,
                'tracking_log_dir': log_dir or self.last_save_dir,
            }
            with open(self._cache_path, 'w') as f:
                json.dump(cache, f, indent=2)
        except Exception as e:
            print(f"Warning: could not save camera cache: {e}")

    # ------------------------------------------------------------------ snapshot / recording

    def take_snapshot(self):
        if self.current_frame is None:
            QtWidgets.QMessageBox.warning(self, tr("Warning"), tr("No frame available."))
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = os.path.join(self.last_save_dir, f"snapshot_{timestamp}.png")
        filepath, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, tr("Save Snapshot"), default_name,
            "PNG Image (*.png);;JPEG Image (*.jpg);;BMP Image (*.bmp)"
        )
        if not filepath:
            return
        self.last_save_dir = os.path.dirname(filepath)
        self._save_localdata_cache()
        annotated = self.current_frame.copy()
        self.draw_marks(annotated)
        cv2.imwrite(filepath, annotated)
        self.status_label.setText(tr("Snapshot saved: {name}", name=os.path.basename(filepath)))

    def start_recording(self):
        if self.is_recording:
            return
        # Try codecs in order: avc1 (H.264 on macOS), mp4v, MJPG/avi
        writer, temp_path = None, None
        for fourcc_str, suffix in [('mp4v', '.mp4'), ('H264', '.mp4'), ('avc1', '.mp4'), ('MJPG', '.avi')]:
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            candidate_path = tmp.name
            tmp.close()
            fourcc = cv2.VideoWriter_fourcc(*fourcc_str)
            candidate = cv2.VideoWriter(
                candidate_path, fourcc, self.fps,
                (self.frame_width, self.frame_height)
            )
            if candidate.isOpened():
                writer, temp_path = candidate, candidate_path
                break
            candidate.release()
            os.remove(candidate_path)
        if writer is None:
            QtWidgets.QMessageBox.critical(self, tr("Error"), tr("Could not start video recording."))
            return
        self.video_writer = writer
        self.video_temp_path = temp_path
        self.is_recording = True
        self.btn_start_record.setEnabled(False)
        self.btn_stop_record.setEnabled(True)
        self.status_label.setText(tr("Recording..."))

    def stop_recording(self):
        if not self.is_recording:
            return
        self.is_recording = False
        self.video_writer.release()
        self.video_writer = None
        self.btn_start_record.setEnabled(True)
        self.btn_stop_record.setEnabled(False)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        recorded_ext = os.path.splitext(self.video_temp_path)[1]  # '.mp4' or '.avi'
        default_name = os.path.join(self.last_save_dir, f"video_{timestamp}{recorded_ext}")
        filepath, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, tr("Save Video"), default_name,
            "MP4 Video (*.mp4);;AVI Video (*.avi)"
        )
        if filepath:
            self.last_save_dir = os.path.dirname(filepath)
            self._save_localdata_cache()
            shutil.move(self.video_temp_path, filepath)
            self.status_label.setText(tr("Video saved: {name}", name=os.path.basename(filepath)))
        else:
            os.remove(self.video_temp_path)
            self.status_label.setText(tr("Recording discarded."))
        self.video_temp_path = None

    # ------------------------------------------------------------------ rendering

    def label_to_frame_coords(self, point):
        x = point.x()
        y = point.y()
        scale = self.render_params.get('scale', 1.0)
        dx = self.render_params.get('dx', 0)
        dy = self.render_params.get('dy', 0)
        fx = int((x - dx) / scale)
        fy = int((y - dy) / scale)
        if fx < 0 or fy < 0 or fx >= self.frame_width or fy >= self.frame_height:
            return -1, -1
        return fx, fy

    def _draw_shape_on_frame(self, frame, shape, color, thickness=None):
        if thickness is None:
            thickness = shape.get('thickness', 1.5)
        thickness = max(1, int(thickness))
        t = shape['type']
        if t == 'circle':
            if 'cx' in shape:
                cv2.circle(frame, (shape['cx'], shape['cy']), max(1, shape['r']), color, thickness)
            else:
                # preview uses x1/y1 as center, x2/y2 as edge point
                x1, y1, x2, y2 = shape['x1'], shape['y1'], shape['x2'], shape['y2']
                r = max(1, int(math.hypot(x2 - x1, y2 - y1)))
                cv2.circle(frame, (x1, y1), r, color, thickness)
        elif t == 'rect':
            x1 = min(shape['x1'], shape['x2'])
            y1 = min(shape['y1'], shape['y2'])
            x2 = max(shape['x1'], shape['x2'])
            y2 = max(shape['y1'], shape['y2'])
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness)
        elif t == 'line':
            cv2.line(frame, (shape['x1'], shape['y1']), (shape['x2'], shape['y2']), color, thickness)
        elif t == 'cross':
            if 'cx' in shape:
                cx, cy, r = shape['cx'], shape['cy'], max(1, shape['r'])
            else:
                cx, cy = shape['x1'], shape['y1']
                r = max(1, int(math.hypot(shape['x2'] - cx, shape['y2'] - cy)))
            cv2.line(frame, (cx - r, cy), (cx + r, cy), color, thickness)
            cv2.line(frame, (cx, cy - r), (cx, cy + r), color, thickness)

    def _draw_timestamp(self, frame):
        ts = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.55
        thickness = 1
        (tw, th), baseline = cv2.getTextSize(ts, font, scale, thickness)
        x = self.frame_width - tw - 8
        y = self.frame_height - baseline - 6
        cv2.putText(frame, ts, (x, y), font, scale, (0, 0, 0), thickness + 1, cv2.LINE_AA)
        cv2.putText(frame, ts, (x, y), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)

    def draw_marks(self, frame):
        if self.show_timestamp:
            self._draw_timestamp(frame)

        if not self.show_all_marks:
            return

        white = (255, 255, 255)
        yellow = (0, 255, 255)   # BGR
        orange = (0, 165, 255)   # preview colour

        for idx, shape in enumerate(self.shapes):
            color = yellow if idx == self.selected_idx else white
            self._draw_shape_on_frame(frame, shape, color)

        # In-progress draw preview
        if self.draw_preview:
            self._draw_shape_on_frame(frame, self.draw_preview, orange)

        # First-point dot for line mode
        if self.line_first_point:
            cv2.circle(frame, self.line_first_point, 4, orange, -1)

    def update_frame(self):
        with self._cap_lock:
            ret, frame = self.cap.read()
        if not ret:
            self.status_label.setText(tr("Error: Could not read frame."))
            return

        self.current_frame = frame.copy()
        self.draw_marks(frame)

        if self.is_recording and self.video_writer:
            self.video_writer.write(frame)
            cv2.circle(frame, (20, 20), 8, (0, 0, 255), -1)

        scale, dx, dy = self._render_to_label(frame, self.video_label)
        self.render_params = {'scale': scale, 'dx': dx, 'dy': dy}
        _tracking_display = self.current_frame.copy()
        self._draw_timestamp(_tracking_display)
        self._render_to_label(_tracking_display, self.tracking_video_label)

        _is_focusing_ch3 = self.autofocus.is_autofocusing()
        _is_focusing_ch7 = self.autofocus_ch7.is_autofocusing()
        _is_focusing = _is_focusing_ch3 or _is_focusing_ch7
        if _is_focusing_ch7 and not _is_focusing_ch3:
            self.autofocus_status_label.setText(tr("Auto focusing in progress. Ch7 is moving."))
        else:
            self.autofocus_status_label.setText(tr("Auto focusing in progress. Ch3 is moving."))
        self.autofocus_status_label.setVisible(_is_focusing)
        self.tab_widget.setTabEnabled(1, not _is_focusing)

        status_text = []
        if self.is_recording:
            status_text.append(tr("REC"))
        if self.is_following:
            status_text.append(tr("FOLLOWING"))
        if self.click_to_move_enabled:
            status_text.append(tr("Click-to-move enabled"))
        if self.calibration_mode:
            status_text.append(tr("Calibration mode"))
        if _is_focusing:
            status_text.append(tr("Auto-focusing..."))
        if self.is_moving:
            status_text.append(tr("Stage moving..."))
        if self.draw_mode:
            status_text.append(tr("Draw [{mode}]", mode=self.draw_mode))
        self.status_label.setText(" | ".join(status_text) if status_text else tr("Ready."))

    def closeEvent(self, event):
        self.radius_popup.hide()
        self._save_shapes()
        self.timer.stop()

        # Ask every in-flight background stage-operation to stop cooperatively.
        self.autofocus.stop_autofocus()
        self.autofocus_ch7.stop_autofocus()
        self.follow_timer.stop()
        self.is_following = False

        # Give them a bounded chance to notice and exit before we pull cap/
        # controller out from under them; escalate to emergency_stop if a
        # thread is still blocked inside controller.wait_until_stop().
        _threads = [self.autofocus.focus_thread, self.autofocus_ch7.focus_thread,
                    self._follow_thread, self._move_thread]
        for t in _threads:
            if t is not None and t.is_alive():
                t.join(timeout=5.0)
        if any(t is not None and t.is_alive() for t in _threads):
            try:
                self.controller.emergency_stop()
            except Exception:
                pass
            for t in _threads:
                if t is not None and t.is_alive():
                    t.join(timeout=2.0)

        # A thread still running past this point means it's genuinely stuck
        # (e.g. blocked inside a socket read); pulling cap/controller out
        # from under it here could crash or corrupt hardware state, so
        # refuse to close instead of proceeding.
        if any(t is not None and t.is_alive() for t in _threads):
            QtWidgets.QMessageBox.warning(
                self, tr("Still Stopping"),
                tr("A background stage operation has not finished stopping yet. "
                   "Please wait a moment and try closing again."),
            )
            event.ignore()
            return

        if self.tracking_csv_file:
            try:
                self.tracking_csv_file.close()
            except Exception:
                pass
            self.tracking_csv_file = None
            self.tracking_csv_path = None
        if self.is_recording and self.video_writer:
            self.is_recording = False
            self.video_writer.release()
            self.video_writer = None
            if self.video_temp_path and os.path.exists(self.video_temp_path):
                os.remove(self.video_temp_path)
        if self.cap:
            self.cap.release()
        cv2.destroyAllWindows()
        if self._owns_controller:
            try:
                self.controller.shutdown()
            except Exception:
                pass
        event.accept()

    # ------------------------------------------------------------------ helpers

    def _render_to_label(self, frame, label):
        lw = max(1, label.width())
        lh = max(1, label.height())
        scale = min(lw / self.frame_width, lh / self.frame_height)
        if scale <= 0:
            scale = 1.0
        nw = int(self.frame_width * scale)
        nh = int(self.frame_height * scale)
        dx = (lw - nw) // 2
        dy = (lh - nh) // 2
        resized = cv2.resize(frame, (nw, nh))
        canvas = np.zeros((lh, lw, 3), dtype=np.uint8)
        canvas[dy:dy + nh, dx:dx + nw] = resized
        qimg = QtGui.QImage(canvas.data, lw, lh, canvas.strides[0],
                            QtGui.QImage.Format.Format_BGR888)
        label.setPixmap(QtGui.QPixmap.fromImage(qimg))
        return scale, dx, dy

    def _compute_xy_shift(self, ref, current):
        return compute_xy_shift(ref, current)

    def _compute_similarity(self, ref, current):
        return compute_similarity(ref, current)

    @QtCore.pyqtSlot(str)
    def _log_tracking_slot(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.tracking_log.append(f"[{ts}] {msg}")

    # ---------------------------------------------------------- tracking tab UI

    def _create_tracking_tab(self):
        widget = QtWidgets.QWidget()
        outer_layout = QtWidgets.QHBoxLayout(widget)

        left_widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(left_widget)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tracking_video_label = QtWidgets.QLabel()
        self.tracking_video_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.tracking_video_label.setStyleSheet("background-color: black;")
        self.tracking_video_label.setMinimumHeight(300)
        self.tracking_video_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Ignored,
        )

        ref_layout = QtWidgets.QHBoxLayout()
        self.btn_take_reference = QtWidgets.QPushButton(tr("Take Reference Photo"))
        self.btn_take_reference.clicked.connect(self.take_reference_photo)
        self.tracking_ref_status = QtWidgets.QLabel(tr("No reference photo taken."))
        ref_layout.addWidget(self.btn_take_reference)
        ref_layout.addWidget(self.tracking_ref_status)
        ref_layout.addStretch()

        log_path_layout = QtWidgets.QHBoxLayout()
        log_path_layout.addWidget(QtWidgets.QLabel(tr("Log directory:")))
        self.tracking_log_path_edit = QtWidgets.QLineEdit(
            self._cached_tracking_log_dir or os.path.expanduser("~"))
        self.tracking_log_path_edit.setToolTip(
            tr("Directory where tracking_log_from_{timestamp}.csv will be saved."))
        self.btn_browse_log = QtWidgets.QPushButton(tr("Browse..."))
        self.btn_browse_log.clicked.connect(self._browse_tracking_log_path)
        log_path_layout.addWidget(self.tracking_log_path_edit)
        log_path_layout.addWidget(self.btn_browse_log)

        save_images_layout = QtWidgets.QHBoxLayout()
        self.chk_save_images = QtWidgets.QCheckBox(
            tr("Save image after every tracking attempt (when similarity threshold is met)"))
        self.chk_save_images.setChecked(False)
        save_images_layout.addWidget(self.chk_save_images)
        save_images_layout.addStretch()

        follow_ctrl_layout = QtWidgets.QHBoxLayout()
        self.btn_start_following = QtWidgets.QPushButton(
            tr("Start Sample Position Tracking by moving Ch3,4,5"))
        self.btn_stop_following = QtWidgets.QPushButton(tr("Stop Tracking"))
        self.btn_start_following.setEnabled(False)
        self.btn_stop_following.setEnabled(False)
        self.btn_start_following.clicked.connect(self.start_following)
        self.btn_stop_following.clicked.connect(self.stop_following)
        self.tracking_warning_label = QtWidgets.QLabel(
            tr("Sample position tracking in progress. Do not move the stages manually."))
        self.tracking_warning_label.setVisible(False)
        _base_pt = QtWidgets.QApplication.font().pointSizeF()
        _large_font = QtGui.QFont()
        _large_font.setPointSizeF(_base_pt * 1.5)
        self.btn_start_following.setFont(_large_font)
        self.btn_stop_following.setFont(_large_font)
        _large_bold = QtGui.QFont(_large_font)
        _large_bold.setBold(True)
        self.tracking_warning_label.setFont(_large_bold)
        self.tracking_warning_label.setStyleSheet("color: red;")
        follow_ctrl_layout.addWidget(self.btn_start_following)
        follow_ctrl_layout.addWidget(self.btn_stop_following)
        follow_ctrl_layout.addWidget(self.tracking_warning_label)
        follow_ctrl_layout.addStretch()

        interval_layout = QtWidgets.QHBoxLayout()
        interval_layout.addWidget(QtWidgets.QLabel(tr("Interval (min):")))
        self.follow_interval_spinbox = _no_wheel(QtWidgets.QDoubleSpinBox())
        self.follow_interval_spinbox.setRange(0.1, 1440.0)
        self.follow_interval_spinbox.setSingleStep(0.5)
        self.follow_interval_spinbox.setValue(1.0)
        self.follow_interval_spinbox.setDecimals(1)
        interval_layout.addWidget(self.follow_interval_spinbox)
        interval_layout.addSpacing(24)
        interval_layout.addWidget(QtWidgets.QLabel(
            tr("Minimum similarity to be satisfied (0–1, 1 is the perfect match with the reference):")))
        self.follow_similarity_spinbox = _no_wheel(QtWidgets.QDoubleSpinBox())
        self.follow_similarity_spinbox.setRange(0.0, 1.0)
        self.follow_similarity_spinbox.setSingleStep(0.01)
        self.follow_similarity_spinbox.setValue(0.95)
        self.follow_similarity_spinbox.setDecimals(2)
        self.follow_similarity_spinbox.setToolTip(
            tr("Normalized cross-correlation similarity (0–1).\n"
               "1.0 = perfect match with reference.\n"
               "If similarity after correction is below this value,\n"
               "XY re-correction is attempted immediately (up to 3 retries)."))
        interval_layout.addWidget(self.follow_similarity_spinbox)
        interval_layout.addStretch()

        af_group = QtWidgets.QGroupBox(tr("Auto-Focus Settings (for Z-correction during tracking)"))
        af_layout = QtWidgets.QHBoxLayout(af_group)
        af_layout.addWidget(QtWidgets.QLabel(tr("Scan Range (um):")))
        self.tr_focus_range_spinbox = _no_wheel(QtWidgets.QSpinBox())
        self.tr_focus_range_spinbox.setRange(2, 200)
        self.tr_focus_range_spinbox.setSingleStep(2)
        self.tr_focus_range_spinbox.setValue(self.focus_range_spinbox.value())
        self.tr_focus_range_spinbox.setToolTip(tr("±scan range in um (1 pulse = 2 um)"))
        af_layout.addWidget(self.tr_focus_range_spinbox)

        af_layout.addWidget(QtWidgets.QLabel(tr("Step (pulse):")))
        self.tr_focus_step_spinbox = _no_wheel(QtWidgets.QSpinBox())
        self.tr_focus_step_spinbox.setRange(1, 10)
        self.tr_focus_step_spinbox.setValue(self.focus_step_spinbox.value())
        af_layout.addWidget(self.tr_focus_step_spinbox)

        af_layout.addWidget(QtWidgets.QLabel(tr("Frames/pos:")))
        self.tr_focus_nframes_spinbox = _no_wheel(QtWidgets.QSpinBox())
        self.tr_focus_nframes_spinbox.setRange(1, 20)
        self.tr_focus_nframes_spinbox.setValue(self.focus_nframes_spinbox.value())
        af_layout.addWidget(self.tr_focus_nframes_spinbox)

        af_layout.addWidget(QtWidgets.QLabel(tr("Speed:")))
        self.tr_focus_speed_group = QtWidgets.QButtonGroup(self)
        self.tr_focus_speed_h = QtWidgets.QRadioButton("H")
        self.tr_focus_speed_m = QtWidgets.QRadioButton("M")
        self.tr_focus_speed_l = QtWidgets.QRadioButton("L")
        self.tr_focus_speed_group.addButton(self.tr_focus_speed_h, 0)
        self.tr_focus_speed_group.addButton(self.tr_focus_speed_m, 1)
        self.tr_focus_speed_group.addButton(self.tr_focus_speed_l, 2)
        self.tr_focus_speed_group.button(self.focus_speed_group.checkedId()).setChecked(True)
        af_layout.addWidget(self.tr_focus_speed_h)
        af_layout.addWidget(self.tr_focus_speed_m)
        af_layout.addWidget(self.tr_focus_speed_l)
        af_layout.addStretch()

        self.tr_focus_range_spinbox.valueChanged.connect(self._on_tr_focus_range_changed)
        self.tr_focus_step_spinbox.valueChanged.connect(self._on_tr_focus_step_changed)
        self.tr_focus_nframes_spinbox.valueChanged.connect(self._on_tr_focus_nframes_changed)
        for _btn in (self.tr_focus_speed_h, self.tr_focus_speed_m, self.tr_focus_speed_l):
            _btn.toggled.connect(lambda checked: self._on_tr_focus_speed_changed() if checked else None)

        attempt_group = QtWidgets.QGroupBox(tr("Per-attempt movement limit (mm)"))
        attempt_layout = QtWidgets.QVBoxLayout(attempt_group)
        for ch, default in [(4, 0.400), (5, 0.400)]:
            attempt_row = QtWidgets.QHBoxLayout()
            attempt_row.addWidget(QtWidgets.QLabel(tr("Ch{ch}:", ch=ch)))
            spin = _no_wheel(QtWidgets.QDoubleSpinBox())
            spin.setRange(0.0, 10.0)
            spin.setSingleStep(0.010)
            spin.setDecimals(3)
            spin.setValue(default)
            setattr(self, f'limit_per_attempt_ch{ch}', spin)
            attempt_row.addWidget(spin)
            attempt_row.addStretch()
            attempt_layout.addLayout(attempt_row)

        total_group = QtWidgets.QGroupBox(
            tr("Total movement limits from start position (mm)"))
        total_grid = QtWidgets.QGridLayout(total_group)
        total_grid.addWidget(QtWidgets.QLabel(""), 0, 0)
        total_grid.addWidget(QtWidgets.QLabel(tr("Min (-)")), 0, 1)
        total_grid.addWidget(QtWidgets.QLabel(tr("Max (+)")), 0, 2)
        _total_defaults = {3: 0.5, 4: 5.0, 5: 50.0}
        for row, ch in enumerate([3, 4, 5], start=1):
            total_grid.addWidget(QtWidgets.QLabel(tr("Ch{ch}:", ch=ch)), row, 0)
            d = _total_defaults[ch]
            for col, (attr, rng, default) in enumerate([
                (f'limit_total_min_ch{ch}', (-100.0, 0.0),  -d),
                (f'limit_total_max_ch{ch}', (  0.0, 100.0),   d),
            ], start=1):
                spin = _no_wheel(QtWidgets.QDoubleSpinBox())
                spin.setRange(*rng)
                spin.setSingleStep(0.1)
                spin.setDecimals(3)
                spin.setValue(default)
                setattr(self, attr, spin)
                total_grid.addWidget(spin, row, col)

        limits_layout = QtWidgets.QHBoxLayout()
        limits_layout.addWidget(attempt_group)
        limits_layout.addWidget(total_group)
        limits_layout.addStretch()

        self.tracking_log = QtWidgets.QTextEdit()
        self.tracking_log.setReadOnly(True)

        layout.addWidget(self.tracking_video_label, stretch=1)
        layout.addLayout(ref_layout)
        layout.addLayout(log_path_layout)
        layout.addLayout(save_images_layout)
        layout.addLayout(interval_layout)
        layout.addWidget(af_group)
        layout.addLayout(limits_layout)
        layout.addLayout(follow_ctrl_layout)

        outer_layout.addWidget(left_widget, stretch=4)
        outer_layout.addWidget(self.tracking_log, stretch=1)
        return widget

    # ------------------------------------------------- tracking business logic

    def _browse_tracking_log_path(self):
        current = self.tracking_log_path_edit.text().strip() or self.last_save_dir
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, tr("Select Log Directory"), current)
        if directory:
            self.tracking_log_path_edit.setText(directory)
            self._save_localdata_cache()

    def take_reference_photo(self):
        if self.current_frame is None:
            QtWidgets.QMessageBox.warning(self, tr("Warning"), tr("No frame available."))
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = os.path.join(self.last_save_dir,
                                    f"reference_{timestamp}.png")
        filepath, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, tr("Save Reference Photo"), default_name,
            "PNG Image (*.png);;JPEG Image (*.jpg)")
        if not filepath:
            return
        ref_dir = os.path.dirname(filepath)
        self.last_save_dir = ref_dir
        self.reference_frame = self.current_frame.copy()
        cv2.imwrite(filepath, self.reference_frame)
        self.tracking_ref_status.setText(
            tr("Reference: {name}", name=os.path.basename(filepath)))
        self.tracking_log_path_edit.setText(ref_dir)
        self._save_localdata_cache()
        self.btn_start_following.setEnabled(True)
        self._log_tracking_slot(
            tr("Reference photo saved: {name}", name=os.path.basename(filepath)))

    def start_following(self):
        if self.reference_frame is None:
            QtWidgets.QMessageBox.warning(
                self, tr("Warning"), tr("Please take a reference photo first."))
            return
        if self.is_following:
            return
        try:
            p3 = self.controller.get_ch_pos(3)
            p4 = self.controller.get_ch_pos(4)
            p5 = self.controller.get_ch_pos(5)
            if None in (p3, p4, p5):
                raise RuntimeError(tr("Could not read motor positions."))
            self.follow_origin_pos = {3: int(p3), 4: int(p4), 5: int(p5)}
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self, tr("Error"), tr("Could not read motor positions: {error}", error=exc))
            return
        # One lease spans the whole tracking session (every _follow_task
        # iteration below reuses it); released in stop_following().
        try:
            self._tracking_lease = self.controller.acquire_motion(
                owner="Interactive Camera", operation="Sample tracking",
            )
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self, tr("Error"), tr("Could not start tracking: {error}", error=exc))
            return
        self.follow_cumulative = {3: 0, 4: 0, 5: 0}
        self.is_following = True
        self.tracking_warning_label.setVisible(True)
        self.btn_start_following.setEnabled(False)
        self.btn_stop_following.setEnabled(True)
        self.tab_widget.setTabEnabled(0, False)

        self.tracking_start_time = datetime.now()
        ts = self.tracking_start_time.strftime('%Y%m%d-%H%M%S')
        log_dir = self.tracking_log_path_edit.text().strip() or os.path.expanduser("~")
        csv_path = os.path.join(log_dir, f"tracking_log_from_{ts}.csv")
        try:
            self.tracking_csv_file = open(csv_path, 'w', newline='', encoding='utf-8')
            self.tracking_csv_writer = csv.writer(self.tracking_csv_file)
            self.tracking_csv_writer.writerow([
                'timestamp', 'elapsed_s',
                'ch3_pulse', 'ch4_pulse', 'ch5_pulse',
                'delta_ch3', 'delta_ch4', 'delta_ch5',
            ])
            self.tracking_csv_file.flush()
            self.tracking_csv_path = csv_path
            self._log_tracking_slot(tr("CSV log: {path}", path=csv_path))
            if self.chk_save_images.isChecked():
                try:
                    img_dir = os.path.join(log_dir, f"images_from_{ts}")
                    os.makedirs(img_dir, exist_ok=True)
                    self.tracking_images_dir = img_dir
                    self.tracking_image_counter = 0
                    self._log_tracking_slot(tr("Images dir: {path}", path=img_dir))
                except Exception as exc2:
                    self._log_tracking_slot(tr("Warning: could not create images dir: {error}", error=exc2))
                    self.tracking_images_dir = None
        except Exception as exc:
            self._log_tracking_slot(tr("Warning: could not open CSV: {error}", error=exc))
            self.tracking_csv_file = None
            self.tracking_csv_writer = None
            self.tracking_csv_path = None
            self.tracking_images_dir = None

        interval_ms = int(self.follow_interval_spinbox.value() * 60 * 1000)
        self.follow_timer.setInterval(interval_ms)
        self.follow_timer.start()
        self._log_tracking_slot(
            tr("Tracking started. Origin Ch3={ch3}, Ch4={ch4}, Ch5={ch5}",
               ch3=self.follow_origin_pos[3], ch4=self.follow_origin_pos[4], ch5=self.follow_origin_pos[5]))

    @QtCore.pyqtSlot()
    def stop_following(self):
        if not self.is_following:
            return
        self.follow_timer.stop()
        self.is_following = False
        lease = getattr(self, "_tracking_lease", None)
        if lease is not None:
            self.controller.release_motion(lease)
            self._tracking_lease = None
        self.tracking_warning_label.setVisible(False)
        self.btn_start_following.setEnabled(True)
        self.btn_stop_following.setEnabled(False)
        self.tab_widget.setTabEnabled(0, True)
        saved_csv = self.tracking_csv_path
        if self.tracking_csv_file:
            try:
                self.tracking_csv_file.close()
            except Exception:
                pass
            self.tracking_csv_file = None
            self.tracking_csv_writer = None
            self.tracking_csv_path = None
        self.tracking_images_dir = None
        _stop_reason = self._follow_stop_reason
        self._follow_stop_reason = None
        _o = self.follow_origin_pos
        _c = self.follow_cumulative
        if _stop_reason:
            _prefix = tr("Tracking automatically stopped ({reason}).", reason=_stop_reason)
        else:
            _prefix = tr("Tracking stopped.")
        self._log_tracking_slot(
            tr("{prefix} "
               "Start: Ch3={s3}, Ch4={s4}, Ch5={s5} [pulse] | "
               "Total movement: ΔCh3={d3:+d}, ΔCh4={d4:+d}, ΔCh5={d5:+d} [pulse] | "
               "End: Ch3={e3}, Ch4={e4}, Ch5={e5} [pulse]",
               prefix=_prefix,
               s3=_o.get(3, 0), s4=_o.get(4, 0), s5=_o.get(5, 0),
               d3=_c[3], d4=_c[4], d5=_c[5],
               e3=_o.get(3, 0) + _c[3], e4=_o.get(4, 0) + _c[4], e5=_o.get(5, 0) + _c[5])
        )
        if saved_csv and os.path.exists(saved_csv):
            self._log_tracking_slot(tr("Saving plots..."))
            threading.Thread(
                target=self._save_tracking_plots,
                args=(saved_csv,),
                daemon=True,
            ).start()

    def _save_tracking_plots(self, csv_path):
        try:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_agg import FigureCanvasAgg
            import matplotlib.ticker as mticker
        except ImportError:
            self._tracking_log_signal.emit(
                tr("Warning: matplotlib not installed — skipping plots."))
            return
        try:
            from datetime import timedelta
            timestamps, times, ch3_vals, ch4_vals, ch5_vals = [], [], [], [], []
            with open(csv_path, 'r', newline='', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    timestamps.append(row['timestamp'])
                    times.append(float(row['elapsed_s']))
                    ch3_vals.append(int(row['ch3_pulse']))
                    ch4_vals.append(int(row['ch4_pulse']))
                    ch5_vals.append(int(row['ch5_pulse']))

            if not times:
                self._tracking_log_signal.emit(tr("No data to plot."))
                return

            # Parse tracking start time for bottom-axis clock labels
            try:
                start_time = datetime.strptime(timestamps[0], '%Y-%m-%d %H:%M:%S.%f')
            except ValueError:
                start_time = datetime.strptime(timestamps[0], '%Y-%m-%d %H:%M:%S')

            plot_dir = os.path.dirname(csv_path)
            stem = os.path.splitext(os.path.basename(csv_path))[0]

            for ch_name, vals in [('Ch3', ch3_vals), ('Ch4', ch4_vals), ('Ch5', ch5_vals)]:
                fig = Figure(figsize=(10, 5))
                canvas = FigureCanvasAgg(fig)

                # ---- bottom axis: actual clock time ----
                ax_bottom = fig.add_subplot(111)
                ax_bottom.plot(times, vals, marker='o', markersize=3, linewidth=1)
                ax_bottom.set_ylabel('Position (pulse)')
                ax_bottom.set_xlabel('Actual time')
                ax_bottom.set_title(f'{ch_name} Position vs Time')
                ax_bottom.grid(True, alpha=0.4)
                ax_bottom.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))

                # Format bottom ticks as HH:MM:SS using elapsed_s → datetime
                def _make_fmt(t0):
                    def _fmt(x, _pos):
                        return (t0 + timedelta(seconds=x)).strftime('%H:%M:%S')
                    return _fmt
                ax_bottom.xaxis.set_major_formatter(
                    mticker.FuncFormatter(_make_fmt(start_time)))
                ax_bottom.tick_params(axis='x', rotation=30)

                # ---- top axis: elapsed time in seconds ----
                ax_top = ax_bottom.twiny()
                ax_top.set_xlabel('Elapsed time (s)')
                ax_top.set_xlim(ax_bottom.get_xlim())

                fig.tight_layout()
                canvas.print_figure(
                    os.path.join(plot_dir, f"{stem}_{ch_name}.png"), dpi=150)

            self._tracking_log_signal.emit(
                tr("Plots saved → {dir} ({stem}_Ch3/4/5.png)", dir=plot_dir, stem=stem))
        except Exception as exc:
            self._tracking_log_signal.emit(tr("Warning: could not save plots: {error}", error=exc))

    def _follow_iteration(self):
        if self.is_following_running or not self.is_following:
            return
        self._follow_thread = threading.Thread(target=self._follow_task, daemon=True)
        self._follow_thread.start()

    def _z_sharpness_scan(self, cum3, tmin3, tmax3, motion):
        """Scan Ch3 (Z/focus) using the same logic as AutoFocus.perform_autofocus:
        move to start_pos, step continuously to end_pos, find sharpness maximum,
        move to best position. Scan range and step size come from self.autofocus."""
        cur3 = self.autofocus.get_current_focus_position()
        if cur3 is None:
            cur3 = self.follow_origin_pos.get(3, 0) + cum3

        scan_range = self.autofocus.focus_range
        step = max(1, self.autofocus.step_size)

        # Clip scan window to total movement limits (absolute pulse coordinates)
        origin3 = self.follow_origin_pos.get(3, 0)
        start = max(cur3 - scan_range, origin3 + tmin3)
        end   = min(cur3 + scan_range, origin3 + tmax3)
        if start >= end:
            return 0

        # Move to scan start (mirrors AutoFocus.focus_routine)
        self.controller.move_ch_absolute(3, start, motion=motion)
        self.controller.wait_until_stop(stay_in_rem=True)

        sharpness_data = []
        pos = start
        while True:
            if not self.is_following:
                return 0
            sharpness_data.append((pos, self.autofocus.measure_sharpness()))
            if pos >= end:
                break
            pos = min(pos + step, end)
            self.controller.move_ch_absolute(3, pos, motion=motion)
            self.controller.wait_until_stop(stay_in_rem=True)

        if not sharpness_data:
            return 0

        best_pos, _ = max(sharpness_data, key=lambda x: x[1])
        self.controller.move_ch_absolute(3, best_pos, motion=motion)
        self.controller.wait_until_stop(stay_in_rem=True)
        return best_pos - cur3

    def _follow_task(self):
        self.is_following_running = True
        motion = getattr(self, "_tracking_lease", None)
        try:
            if not self.is_following or self.current_frame is None:
                return
            if motion is None:
                raise RuntimeError("Tracking session has no motion lease")

            self.controller.switch_to_rem(motion=motion)

            tmin3 = int(
                self.limit_total_min_ch3.value() * 1000 / UM_PER_PULSE_CH3)
            tmax3 = int(
                self.limit_total_max_ch3.value() * 1000 / UM_PER_PULSE_CH3)
            d_ch3 = 0

            if self.autofocus.focus_range > 0 and self.is_following:
                d_ch3 += self._z_sharpness_scan(
                    self.follow_cumulative[3] + d_ch3, tmin3, tmax3, motion)

            d_ch4 = d_ch5 = 0
            lim4 = lim5 = tmin4 = tmax4 = tmin5 = tmax5 = 0
            if self.calibration_data and self.is_following:
                with self._cap_lock:
                    ret, focused_frame = self.cap.read()
                if not ret:
                    focused_frame = self.current_frame.copy()

                dx_px, dy_px = self._compute_xy_shift(
                    self.reference_frame, focused_frame)
                M_inv = np.array(self.calibration_data['matrix_inv'])
                motor_disp = -(M_inv @ np.array([dx_px, dy_px]))
                d_ch4 = int(motor_disp[0])
                d_ch5 = int(motor_disp[1])

                lim4 = max(0, int(
                    self.limit_per_attempt_ch4.value() * 1000 / UM_PER_PULSE_CH4))
                lim5 = max(0, int(
                    self.limit_per_attempt_ch5.value() * 1000 / UM_PER_PULSE_CH5))
                d_ch4 = max(-lim4, min(lim4, d_ch4))
                d_ch5 = max(-lim5, min(lim5, d_ch5))

                tmin4 = int(
                    self.limit_total_min_ch4.value() * 1000 / UM_PER_PULSE_CH4)
                tmax4 = int(
                    self.limit_total_max_ch4.value() * 1000 / UM_PER_PULSE_CH4)
                tmin5 = int(
                    self.limit_total_min_ch5.value() * 1000 / UM_PER_PULSE_CH5)
                tmax5 = int(
                    self.limit_total_max_ch5.value() * 1000 / UM_PER_PULSE_CH5)
                new4 = max(tmin4, min(tmax4,
                                      self.follow_cumulative[4] + d_ch4))
                d_ch4 = new4 - self.follow_cumulative[4]
                new5 = max(tmin5, min(tmax5,
                                      self.follow_cumulative[5] + d_ch5))
                d_ch5 = new5 - self.follow_cumulative[5]

                if d_ch4 != 0:
                    self.controller.move_ch_relative(4, d_ch4, motion=motion)
                if d_ch5 != 0:
                    self.controller.move_ch_relative(5, d_ch5, motion=motion)
                if d_ch4 != 0 or d_ch5 != 0:
                    self.controller.wait_until_stop(stay_in_rem=True)

            if self.autofocus.focus_range > 0 and self.is_following:
                d_ch3 += self._z_sharpness_scan(
                    self.follow_cumulative[3] + d_ch3, tmin3, tmax3, motion)

            # --- similarity check + immediate re-correction (max 3 retries) ---
            # TM_CCOEFF_NORMED returns 0–1; 1.0 = perfect match.
            # Threshold of 0.95 ≈ tolerating ~3–5 % positional drift in image space.
            _SIMILARITY_MAX_RETRIES = 3
            _threshold_satisfied = False
            _last_frame = None
            if self.calibration_data and self.is_following:
                _sim_threshold = self.follow_similarity_spinbox.value()
                for _retry in range(_SIMILARITY_MAX_RETRIES):
                    with self._cap_lock:
                        _ret, _chk = self.cap.read()
                    if not _ret:
                        break
                    _sim = self._compute_similarity(self.reference_frame, _chk)
                    self._tracking_log_signal.emit(tr("Similarity: {sim:.3f}", sim=_sim))
                    if _sim >= _sim_threshold:
                        _threshold_satisfied = True
                        _last_frame = _chk
                        break
                    self._tracking_log_signal.emit(
                        tr("Similarity below threshold ({threshold:.2f}) — "
                           "re-correcting XY (attempt {attempt}/{max_retries})",
                           threshold=_sim_threshold, attempt=_retry + 1, max_retries=_SIMILARITY_MAX_RETRIES))
                    _dx, _dy = self._compute_xy_shift(self.reference_frame, _chk)
                    _Minv = np.array(self.calibration_data['matrix_inv'])
                    _disp = -(_Minv @ np.array([_dx, _dy]))
                    _d4 = max(-lim4, min(lim4, int(_disp[0])))
                    _d5 = max(-lim5, min(lim5, int(_disp[1])))
                    _new4 = max(tmin4, min(tmax4,
                                           self.follow_cumulative[4] + d_ch4 + _d4))
                    _d4 = _new4 - (self.follow_cumulative[4] + d_ch4)
                    _new5 = max(tmin5, min(tmax5,
                                           self.follow_cumulative[5] + d_ch5 + _d5))
                    _d5 = _new5 - (self.follow_cumulative[5] + d_ch5)
                    if _d4 != 0:
                        self.controller.move_ch_relative(4, _d4, motion=motion)
                    if _d5 != 0:
                        self.controller.move_ch_relative(5, _d5, motion=motion)
                    if _d4 != 0 or _d5 != 0:
                        self.controller.wait_until_stop(stay_in_rem=True)
                    d_ch4 += _d4
                    d_ch5 += _d5

            # Save snapshot when threshold is met and image saving is enabled
            if _threshold_satisfied and self.tracking_images_dir and _last_frame is not None:
                try:
                    self.tracking_image_counter += 1
                    _img_ts = datetime.now().strftime('%Y%m%d-%H%M%S')
                    _fname = f"frame_{self.tracking_image_counter:04d}_{_img_ts}.png"
                    _save_frame = _last_frame.copy()
                    self._draw_timestamp(_save_frame)
                    cv2.imwrite(
                        os.path.join(self.tracking_images_dir, _fname), _save_frame)
                    self._tracking_log_signal.emit(tr("Image saved: {name}", name=_fname))
                except Exception as _exc:
                    self._tracking_log_signal.emit(tr("Warning: could not save image: {error}", error=_exc))

            # self.controller.switch_to_loc()

            self.follow_cumulative[3] += d_ch3
            self.follow_cumulative[4] += d_ch4
            self.follow_cumulative[5] += d_ch5

            # --- total movement limit check ---
            # Stop tracking if any channel's cumulative displacement has reached
            # its configured limit (stage collision boundary).
            _limit_hits = []
            if self.autofocus.focus_range > 0:
                if self.follow_cumulative[3] <= tmin3:
                    _limit_hits.append(
                        tr("Ch{ch} min ({val:.3f} mm)", ch=3, val=tmin3 * UM_PER_PULSE_CH3 / 1000))
                elif self.follow_cumulative[3] >= tmax3:
                    _limit_hits.append(
                        tr("Ch{ch} max ({val:.3f} mm)", ch=3, val=tmax3 * UM_PER_PULSE_CH3 / 1000))
            if self.calibration_data:
                if self.follow_cumulative[4] <= tmin4:
                    _limit_hits.append(
                        tr("Ch{ch} min ({val:.3f} mm)", ch=4, val=tmin4 * UM_PER_PULSE_CH4 / 1000))
                elif self.follow_cumulative[4] >= tmax4:
                    _limit_hits.append(
                        tr("Ch{ch} max ({val:.3f} mm)", ch=4, val=tmax4 * UM_PER_PULSE_CH4 / 1000))
                if self.follow_cumulative[5] <= tmin5:
                    _limit_hits.append(
                        tr("Ch{ch} min ({val:.3f} mm)", ch=5, val=tmin5 * UM_PER_PULSE_CH5 / 1000))
                elif self.follow_cumulative[5] >= tmax5:
                    _limit_hits.append(
                        tr("Ch{ch} max ({val:.3f} mm)", ch=5, val=tmax5 * UM_PER_PULSE_CH5 / 1000))
            if _limit_hits:
                self._follow_stop_reason = tr("Total limit exceeded: {hits}", hits=", ".join(_limit_hits))
                QtCore.QMetaObject.invokeMethod(
                    self, "stop_following",
                    QtCore.Qt.ConnectionType.QueuedConnection)
                return

            if self.tracking_csv_writer and self.tracking_start_time:
                elapsed = (datetime.now() - self.tracking_start_time).total_seconds()
                ch3_abs = self.follow_origin_pos.get(3, 0) + self.follow_cumulative[3]
                ch4_abs = self.follow_origin_pos.get(4, 0) + self.follow_cumulative[4]
                ch5_abs = self.follow_origin_pos.get(5, 0) + self.follow_cumulative[5]
                try:
                    self.tracking_csv_writer.writerow([
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                        f'{elapsed:.3f}',
                        ch3_abs, ch4_abs, ch5_abs,
                        d_ch3, d_ch4, d_ch5,
                    ])
                    self.tracking_csv_file.flush()
                except Exception as exc:
                    print(f"CSV write error: {exc}")

            self._tracking_log_signal.emit(
                tr("ΔCh3={d3:+d}, ΔCh4={d4:+d}, ΔCh5={d5:+d} [pulse] | "
                   "Total: Ch3={t3:+d}, Ch4={t4:+d}, Ch5={t5:+d}",
                   d3=d_ch3, d4=d_ch4, d5=d_ch5,
                   t3=self.follow_cumulative[3], t4=self.follow_cumulative[4], t5=self.follow_cumulative[5]))

        except Exception as exc:
            print(f"Follow task error: {exc}")
            try:
                if motion is not None and self.controller.coordinator.is_valid(motion):
                    self.controller.switch_to_loc(motion=motion)
            except Exception:
                pass
            self._tracking_log_signal.emit(tr("Error: {msg}", msg=exc))
        finally:
            self.is_following_running = False


def main():
    app = QtWidgets.QApplication(sys.argv)
    try:
        window = MainWindow()
    except Exception:
        sys.exit(1)
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

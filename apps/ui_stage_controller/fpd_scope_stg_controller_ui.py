import sys
import json
import os
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QGridLayout, QGroupBox, QLabel,
                             QLineEdit, QPushButton, QRadioButton, QSizePolicy,
                             QButtonGroup, QMessageBox, QDialog)
from PyQt6.QtCore import Qt, QTimer, QEvent, pyqtSignal, QObject, QPointF, QRectF
from PyQt6.QtGui import QPainter, QPen, QColor, QPolygonF

try:
    from utils.stage.control_stage import PM16CController, PULSE_SCALE, CH9_CH8_SAFE_BOUNDARY
    from utils.stage.control_stage_sim import PM16CControllerSim
except ImportError:
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from utils.stage.control_stage import PM16CController, PULSE_SCALE, CH9_CH8_SAFE_BOUNDARY
    from utils.stage.control_stage_sim import PM16CControllerSim

try:
    from settings.i18n import tr
except ImportError:
    import os as _os, sys as _sys
    _root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from settings.i18n import tr

# --- ボタンのスタイル定義 ---
STYLE_DET_IN = """
    QPushButton { background-color: #FF00FF; color: white; font-weight: bold; border: none; border-radius: 3px; padding-right: 8px; padding-left: 8px; }
    QPushButton:disabled { background-color: #A0A0A0; color: #FFFFFF; border: none; }
"""
STYLE_DET_OUT = """
    QPushButton { background-color: transparent; color: black; font-weight: bold; border: 1.5px solid #FF00FF; border-radius: 3px;  padding-right: 8px; padding-left: 8px; }
    QPushButton:disabled { background-color: transparent; color: #A0A0A0; border: 3px solid #A0A0A0; }
"""
STYLE_MIC_IN = """
    QPushButton { background-color: #00BFFF; color: white; font-weight: bold; border: none; border-radius: 3px; }
    QPushButton:disabled { background-color: #A0A0A0; color: #FFFFFF; border: none; }
"""
STYLE_MIC_OUT = """
    QPushButton { background-color: transparent; color: clack; font-weight: bold; border: 1.5px solid #00BFFF; border-radius: 3px; }
    QPushButton:disabled { background-color: transparent; color: #A0A0A0; border: 3px solid #A0A0A0; }
"""
STYLE_GREEN = """
    QPushButton { background-color: #4CAF50; color: white; font-weight: bold; }
    QPushButton:disabled { background-color: #A0A0A0; color: #FFFFFF; }
"""
STYLE_SHORTCUT = """
    QPushButton { font-weight: bold; font-size: 14pt; }
    QPushButton:disabled { background-color: #A0A0A0; color: #FFFFFF; }
"""
STYLE_NSTOP = """
    QPushButton { background-color: #FF8C00; color: white; font-weight: bold; font-size: 14px; border-radius: 4px; }
    QPushButton:pressed { background-color: #CC7000; }
"""
STYLE_ESTOP = """
    QPushButton { background-color: #FF3333; color: white; font-weight: bold; font-size: 16px; border-radius: 4px; }
    QPushButton:pressed { background-color: #CC0000; }
"""

# --- 可視化用固定パルス位置 (UIの入力値には依存しない) ---
DET_VIZ_OUT_PULSE = -40000   # Detector OUT 端
DET_VIZ_IN_PULSE  =   4000   # Detector IN  端
MIC_VIZ_OUT_PULSE =      0   # Microscope OUT 端
MIC_VIZ_IN_PULSE  = 287450   # Microscope IN  端

# --- Ch9/Ch8 パルス-距離変換係数 (PULSE_SCALE から取得) ---
CH9_UM_PER_PULSE = PULSE_SCALE[9]
CH8_UM_PER_PULSE = PULSE_SCALE[8]

# --- ローカル設定ファイル ---
_SETTINGS_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__localdata")
_SETTINGS_FILE = os.path.join(_SETTINGS_DIR, "stage_settings.json")
_SETTINGS_DEFAULTS = {
    "det_out": "-40000",
    "det_in":  "1779",
    "ch6":     "12000",
    "ch7":     "120000",
    "ch8_out": "0",
    "ch8_in":  "281092",
}

_CAMERA_CREDS_FILE     = os.path.join(_SETTINGS_DIR, "camera_credentials.json")
_CAMERA_CREDS_DEFAULTS = {"username": "BL18Ccamera2", "password": "bl-18c"}


def _load_camera_credentials():
    try:
        with open(_CAMERA_CREDS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {**_CAMERA_CREDS_DEFAULTS, **data}
    except (FileNotFoundError, json.JSONDecodeError):
        os.makedirs(_SETTINGS_DIR, exist_ok=True)
        with open(_CAMERA_CREDS_FILE, "w", encoding="utf-8") as f:
            json.dump(_CAMERA_CREDS_DEFAULTS, f, indent=2, ensure_ascii=False)
        return _CAMERA_CREDS_DEFAULTS.copy()


# --- コントローラ状態ポーラー (メインスレッドの QTimer で定期取得) ---
class ControllerPoller(QObject):
    positionChanged = pyqtSignal(int, int)   # channel, current_pulse
    movementStateChanged = pyqtSignal(bool)

    _CHANNELS = [6, 7, 8, 9]

    def __init__(self, controller):
        super().__init__()
        self._controller = controller
        self._was_moving = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

    def start(self):
        self._timer.start(300)

    def stop(self):
        self._timer.stop()

    def _poll(self):
        try:
            # The controller-owned StageStateMonitor performs all socket I/O.
            # This QTimer runs on the GUI thread and therefore only reads the
            # shared in-memory cache; a slow/timed-out PM16C reply can no
            # longer freeze this window.
            is_moving = self._controller.get_cached_is_moving()
            just_stopped = (not is_moving and self._was_moving)
            if is_moving != self._was_moving:
                self.movementStateChanged.emit(is_moving)
                self._was_moving = is_moving
            # Reading the cache is cheap and keeps idle-time external/preset
            # position changes visible too.  It performs no PM16C I/O.
            states = self._controller.get_cached_states(self._CHANNELS)
            for ch, state in states.items():
                self.positionChanged.emit(ch, state.position)
        except Exception as e:
            print(f"[Poller] Error: {e}")


# --- カスタム描画ウィジェット (ステージの視覚化) ---
class StageVisualizationView(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(450, 450)
        self._det_pos_um = float(DET_VIZ_OUT_PULSE * CH9_UM_PER_PULSE)
        self._mic_pos_um = float(MIC_VIZ_OUT_PULSE * CH8_UM_PER_PULSE)

    def set_detector_pulse(self, current_pulse):
        self._det_pos_um = float(current_pulse * CH9_UM_PER_PULSE)
        self.update()

    def set_microscope_pulse(self, current_pulse):
        self._mic_pos_um = float(current_pulse * CH8_UM_PER_PULSE)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), Qt.GlobalColor.white)

        w = self.width()
        h = self.height()
        m = 30

        det_w = 90
        det_h = 100
        mic_w = 110
        mic_h = 70

        # 物理レンジ (µm)
        det_out_um   = float(DET_VIZ_OUT_PULSE * CH9_UM_PER_PULSE)   # -400000 µm
        det_in_um    = float(DET_VIZ_IN_PULSE  * CH9_UM_PER_PULSE)   #   40000 µm
        mic_out_um   = float(MIC_VIZ_OUT_PULSE * CH8_UM_PER_PULSE)   #       0 µm
        mic_in_um    = float(MIC_VIZ_IN_PULSE  * CH8_UM_PER_PULSE)   #  287450 µm
        det_range_um = det_in_um - det_out_um   # 440000 µm (440 mm)
        mic_range_um = mic_in_um - mic_out_um   # 287450 µm (287 mm)

        in_x      = w / 2
        det_out_x = float(m + 20)

        # 共通スケール: Detector の水平トラックに合わせて px/µm を決定
        avail_det_px = in_x - det_out_x
        px_per_um    = avail_det_px / det_range_um if det_range_um > 0 else 1.0

        # Microscope のトラック高さ = 物理レンジに比例
        mic_track_px = mic_range_um * px_per_um

        # Y配置: 両トラックをウィジェット中央に寄せる
        in_y      = max(float(m + det_h / 2), (h - mic_track_px) / 2)
        mic_out_y = in_y + mic_track_px

        # 0-1 比率 (クランプ済み)
        det_ratio = max(0.0, min(1.0,
            (self._det_pos_um - det_out_um) / det_range_um if det_range_um else 0.0))
        mic_ratio = max(0.0, min(1.0,
            (self._mic_pos_um - mic_out_um) / mic_range_um if mic_range_um else 0.0))

        # 0. 通過領域の背景 (不透明度 10%)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 0, 255, 25))
        painter.drawRect(QRectF(det_out_x - det_w / 2, in_y - det_h / 2,
                                in_x + det_w / 2 - (det_out_x - det_w / 2), det_h))
        painter.setBrush(QColor(0, 191, 255, 25))
        painter.drawRect(QRectF(in_x - mic_w / 2, in_y - mic_h / 2,
                                mic_w, mic_out_y - in_y + mic_h))

        # 1. X-ray 入射矢印
        pen_xray = QPen(QColor(255, 140, 0), 4)
        painter.setPen(pen_xray)
        xray_start_x = w - m
        xray_end_x   = in_x + 80
        painter.drawLine(xray_start_x, int(in_y), int(xray_end_x), int(in_y))
        arrow_head_xray = QPolygonF([
            QPointF(xray_end_x + 15, in_y - 8),
            QPointF(xray_end_x, in_y),
            QPointF(xray_end_x + 15, in_y + 8)
        ])
        painter.setBrush(QColor(255, 140, 0))
        painter.drawPolygon(arrow_head_xray)
        painter.setPen(Qt.GlobalColor.black)
        font = painter.font()
        font.setBold(True)
        font.setPointSize(12)
        painter.setFont(font)
        painter.drawText(int(xray_start_x - 60), int(in_y - 15), tr("X-ray"))

        # 2. 移動ガイドライン
        font.setPointSize(9)
        font.setBold(False)
        painter.setFont(font)
        pen_pink = QPen(QColor(255, 0, 255), 2, Qt.PenStyle.DashLine)
        painter.setPen(pen_pink)
        painter.drawLine(int(det_out_x), int(in_y), int(in_x), int(in_y))
        pen_blue = QPen(QColor(0, 191, 255), 2, Qt.PenStyle.DashLine)
        painter.setPen(pen_blue)
        painter.drawLine(int(in_x), int(mic_out_y), int(in_x), int(in_y))

        # 3. Detector (Ch9) の描画
        cur_det_x = det_out_x + det_ratio * (in_x - det_out_x)
        det_rect  = QRectF(cur_det_x - det_w / 2, in_y - det_h / 2, det_w, det_h)
        painter.setPen(QPen(QColor(255, 0, 255), 3))
        painter.setBrush(QColor(255, 255, 255, 255))
        painter.drawRect(det_rect)
        painter.setPen(Qt.GlobalColor.black)
        painter.drawText(det_rect, Qt.AlignmentFlag.AlignCenter, tr("Detector\n(Ch9)"))

        # 4. Microscope (Ch6, 7, 8) の描画
        cur_mic_y = mic_out_y + mic_ratio * (in_y - mic_out_y)
        mic_rect  = QRectF(in_x - mic_w / 2, cur_mic_y - mic_h / 2, mic_w, mic_h)
        painter.setPen(QPen(QColor(0, 191, 255), 3))
        painter.setBrush(QColor(255, 255, 255, 255))
        painter.drawRect(mic_rect)
        painter.setPen(Qt.GlobalColor.black)
        painter.drawText(mic_rect, Qt.AlignmentFlag.AlignCenter, tr("Microscope\n(Ch6,7,8)"))


# --- 移動中カメラポップアップ ---
class MovingCameraPopup(QDialog):
    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(tr("Stage Moving"))
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)

        layout = QVBoxLayout(self)

        lbl_status = QLabel(tr("Stage is moving..."))
        lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = lbl_status.font()
        font.setPointSize(16)
        font.setBold(True)
        lbl_status.setFont(font)
        layout.addWidget(lbl_status)

        btn_layout = QHBoxLayout()
        btn_nstop = QPushButton(tr("Stop all stages\n(slow stop)"))
        btn_nstop.setStyleSheet(STYLE_NSTOP)
        btn_nstop.setMinimumHeight(60)
        btn_estop = QPushButton(tr("EMERGENCY STOP\n(Immediate halt)"))
        btn_estop.setStyleSheet(STYLE_ESTOP)
        btn_estop.setMinimumHeight(60)
        btn_layout.addWidget(btn_nstop)
        btn_layout.addWidget(btn_estop)
        layout.addLayout(btn_layout)

        btn_nstop.clicked.connect(parent._on_nstop)
        btn_estop.clicked.connect(parent._on_estop)

        self.adjustSize()
        self.setMinimumWidth(400)

    def closeEvent(self, event):
        super().closeEvent(event)


# --- メインウィンドウ ---
class Bl18cStageControlApp(QMainWindow):
    def __init__(self, controller=None):
        super().__init__()
        self.setWindowTitle(tr("BL-18C FPD + Scope Stage Control"))
        self.resize(1100, 800)

        if controller is not None:
            self.controller = controller
            self._owns_controller = False
        else:
            self.controller = PM16CController(ip='192.168.1.55', port=7777, debug=True)
            try:
                self.controller.connect()
            except Exception as exc:
                ret = QMessageBox.critical(
                    None, tr("Connection Error"),
                    tr("Could not connect to stage controller:\n{error}\n\n"
                       "Run in simulation mode instead?", error=exc),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if ret == QMessageBox.StandardButton.Yes:
                    self.controller = PM16CControllerSim(debug=True)
                    self.controller.connect()
                else:
                    raise
            self._owns_controller = True

        self.speed_groups = {}  # {ch_num: QButtonGroup}
        self.interactive_controls = []
        self._pending_move = None
        self._is_moving = False
        self._moving_popup = None
        self._shortcut_active = False
        self._last_moved_ch = None
        self._step1_verify_ch = None
        self._step1_verify_target = None
        self._step1_verify_speed = "H"
        self._step1_retry_done = False
        self._initial_refresh_retries = 0

        self.init_ui()
        self._load_settings()

        self._poller = ControllerPoller(self.controller)
        self._poller.positionChanged.connect(self.update_position_display)
        self._poller.movementStateChanged.connect(self._on_movement_state_changed)
        self._poller.start()
        QTimer.singleShot(100, self._refresh_positions)

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # --- 左側: コントロールパネル ---
        left_panel = QVBoxLayout()
        main_layout.addLayout(left_panel)

        # 1. Detector (Ch9)
        det_group = QGroupBox(tr("Detector (Ch9)"))
        det_layout = QGridLayout()
        det_layout.setVerticalSpacing(3)
        self.line_det_out = QLineEdit(); self.line_det_out.setFixedWidth(80)
        self.btn_det_out = QPushButton(tr("OUT")); self.btn_det_out.setStyleSheet(STYLE_DET_OUT)
        self.line_det_in = QLineEdit(); self.line_det_in.setFixedWidth(80)
        self.btn_det_in = QPushButton(tr("IN")); self.btn_det_in.setStyleSheet(STYLE_DET_IN)

        det_layout.addWidget(QLabel(tr("OUT pos.")), 0, 0)
        det_layout.addWidget(self.line_det_out, 0, 1)
        det_layout.addWidget(self.btn_det_out, 0, 2)
        det_layout.addWidget(QLabel(tr("IN pos.")), 1, 0)
        det_layout.addWidget(self.line_det_in, 1, 1)
        det_layout.addWidget(self.btn_det_in, 1, 2)
        det_layout.addWidget(self._make_speed_widget(9, default="H"), 0, 3, 2, 1)

        det_group.setLayout(det_layout)
        det_group.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        left_panel.addWidget(det_group)

        self.interactive_controls.extend([self.line_det_out, self.btn_det_out,
                                          self.line_det_in, self.btn_det_in])
        self.line_det_out.textChanged.connect(self.on_det_out_changed)
        self.line_det_in.textChanged.connect(self.on_det_in_changed)

        # 2. Stage Visualization Area
        self.viz_view = StageVisualizationView()
        left_panel.addWidget(self.viz_view, 1)

        # 3. Microscope (Ch6, 7, 8)
        mic_group = QGroupBox(tr("Microscope (Ch6, 7, 8)"))
        mic_layout = QGridLayout()
        mic_layout.setVerticalSpacing(3)

        self.line_ch6 = QLineEdit(); self.line_ch6.setFixedWidth(80)
        self.btn_ch6_move = QPushButton(tr("Move")); self.btn_ch6_move.setStyleSheet(STYLE_GREEN)
        self.line_ch7 = QLineEdit(); self.line_ch7.setFixedWidth(80)
        self.btn_ch7_move = QPushButton(tr("Move")); self.btn_ch7_move.setStyleSheet(STYLE_GREEN)
        self.line_ch8_out = QLineEdit(); self.line_ch8_out.setFixedWidth(80)
        self.btn_ch8_out = QPushButton(tr("OUT")); self.btn_ch8_out.setStyleSheet(STYLE_MIC_OUT)
        self.line_ch8_in = QLineEdit(); self.line_ch8_in.setFixedWidth(80)
        self.btn_ch8_in = QPushButton(tr("IN")); self.btn_ch8_in.setStyleSheet(STYLE_MIC_IN)

        mic_layout.addWidget(QLabel(tr("Ch6 Target:")), 0, 0)
        mic_layout.addWidget(self.line_ch6, 0, 1)
        mic_layout.addWidget(self.btn_ch6_move, 0, 2)
        mic_layout.addWidget(self._make_speed_widget(6), 0, 3)

        mic_layout.addWidget(QLabel(tr("Ch7 Target:")), 1, 0)
        mic_layout.addWidget(self.line_ch7, 1, 1)
        mic_layout.addWidget(self.btn_ch7_move, 1, 2)
        mic_layout.addWidget(self._make_speed_widget(7), 1, 3)

        mic_layout.addWidget(QLabel(tr("Ch8 IN pos.")), 2, 0)
        mic_layout.addWidget(self.line_ch8_in, 2, 1)
        mic_layout.addWidget(self.btn_ch8_in, 2, 2)
        mic_layout.addWidget(QLabel(tr("Ch8 OUT pos.")), 3, 0)
        mic_layout.addWidget(self.line_ch8_out, 3, 1)
        mic_layout.addWidget(self.btn_ch8_out, 3, 2)
        mic_layout.addWidget(self._make_speed_widget(8), 2, 3, 2, 1)

        mic_group.setLayout(mic_layout)
        mic_group.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        left_panel.addWidget(mic_group)

        self.interactive_controls.extend([
            self.line_ch6, self.btn_ch6_move,
            self.line_ch7, self.btn_ch7_move,
            self.line_ch8_out, self.btn_ch8_out,
            self.line_ch8_in, self.btn_ch8_in,
        ])
        self.line_ch6.textChanged.connect(self.on_ch6_changed)
        self.line_ch7.textChanged.connect(self.on_ch7_changed)
        self.line_ch8_out.textChanged.connect(self.on_ch8_out_changed)
        self.line_ch8_in.textChanged.connect(self.on_ch8_in_changed)

        self.btn_det_out.clicked.connect(self.move_det_out)
        self.btn_det_in.clicked.connect(self.move_det_in)
        self.btn_ch6_move.clicked.connect(self.move_ch6)
        self.btn_ch7_move.clicked.connect(self.move_ch7)
        self.btn_ch8_out.clicked.connect(self.move_ch8_out)
        self.btn_ch8_in.clicked.connect(self.move_ch8_in)

        # --- 右側: Shortcuts, E-Stop ---
        right_panel = QVBoxLayout()
        main_layout.addLayout(right_panel, 0)

        # Shortcuts
        shortcut_group = QGroupBox(tr("Shortcuts:"))
        shortcut_layout = QVBoxLayout()
        self.btn_short1 = QPushButton(tr("See the sample by the microscope:\nDetector→OUT and Microscope→IN (High SPD)")); self.btn_short1.setMinimumHeight(60)
        self.btn_short2 = QPushButton(tr("Take XRD Data:\nMicroscope→OUT and Detector→IN (High SPD)")); self.btn_short2.setMinimumHeight(60)
        self.btn_short1.setStyleSheet(STYLE_SHORTCUT); self.btn_short2.setStyleSheet(STYLE_SHORTCUT)
        shortcut_layout.addWidget(self.btn_short1); shortcut_layout.addWidget(self.btn_short2)
        shortcut_group.setLayout(shortcut_layout)
        right_panel.addWidget(shortcut_group)

        self.interactive_controls.extend([self.btn_short1, self.btn_short2])
        self.btn_short1.clicked.connect(self.shortcut_1)
        self.btn_short2.clicked.connect(self.shortcut_2)

        # Current Position (semi-live, driven by ControllerPoller / _refresh_positions)
        pos_group = QGroupBox(tr("Current Position"))
        pos_layout = QVBoxLayout()
        self.lbl_pos = {}
        for ch in (6, 7, 8, 9):
            lbl = QLabel(f"Ch{ch} ----")
            # theme.py's app-wide QSS ("QWidget { font-size: 13px; }") overrides
            # setFont() once any stylesheet is active — only a widget's own
            # local font-size (px/pt; QSS doesn't support "em") reliably wins.
            lbl.setStyleSheet("font-size: 16px;")
            pos_layout.addWidget(lbl)
            self.lbl_pos[ch] = lbl
        pos_group.setLayout(pos_layout)
        right_panel.addWidget(pos_group)

        self.lbl_status = QLabel("")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.lbl_status.setStyleSheet("color: #1a6fbf; font-size: 18px; padding: 4px 2px 0px 2px;")
        self.lbl_status.setWordWrap(True)
        right_panel.addWidget(self.lbl_status)

        right_panel.addStretch()

        # Normal Stop
        self.btn_nstop = QPushButton(tr("Stop all stages (slow stop)"))
        self.btn_nstop.setStyleSheet(STYLE_NSTOP)
        self.btn_nstop.setMinimumHeight(60)
        self.btn_nstop.clicked.connect(self._on_nstop)
        right_panel.addWidget(self.btn_nstop)

        # Emergency Stop
        self.btn_estop = QPushButton(tr("EMERGENCY STOP\n(Immediate halt)"))
        self.btn_estop.setStyleSheet(STYLE_ESTOP)
        self.btn_estop.setMinimumHeight(60)
        self.btn_estop.clicked.connect(self._on_estop)
        right_panel.addWidget(self.btn_estop)

    def _make_speed_widget(self, ch_num, default="M"):
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(QLabel(tr("Spd:")))
        btn_group = QButtonGroup(self)
        self.speed_groups[ch_num] = btn_group
        # Values double as the parsed PM16C speed-register code ("H"/"M"/"L") compared
        # via .text() in _get_speed()/_refresh_positions() — kept in English regardless
        # of UI language, like the Free 2D Scan Speed radio buttons.
        r_h = QRadioButton("H"); r_m = QRadioButton("M"); r_l = QRadioButton("L")
        btn_group.addButton(r_h); btn_group.addButton(r_m); btn_group.addButton(r_l)
        (r_h if default == "H" else r_m).setChecked(True)
        layout.addWidget(r_h); layout.addWidget(r_m); layout.addWidget(r_l)
        self.interactive_controls.extend([r_h, r_m, r_l])
        return container


    # ==========================================
    # --- 入力値変更時のハンドラ ---
    # ==========================================
    def on_det_out_changed(self, _):
        self._save_settings()

    def on_det_in_changed(self, _):
        self._save_settings()

    def on_ch6_changed(self, _):
        self._save_settings()

    def on_ch7_changed(self, _):
        self._save_settings()

    def on_ch8_out_changed(self, _):
        self._save_settings()

    def on_ch8_in_changed(self, _):
        self._save_settings()

    # --- 設定の読み書き ---
    def _load_settings(self):
        try:
            with open(_SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        d = {**_SETTINGS_DEFAULTS, **data}
        self.line_det_out.setText(d["det_out"])
        self.line_det_in.setText(d["det_in"])
        self.line_ch6.setText(d["ch6"])
        self.line_ch7.setText(d["ch7"])
        self.line_ch8_out.setText(d["ch8_out"])
        self.line_ch8_in.setText(d["ch8_in"])

    def _save_settings(self):
        data = {
            "det_out": self.line_det_out.text(),
            "det_in":  self.line_det_in.text(),
            "ch6":     self.line_ch6.text(),
            "ch7":     self.line_ch7.text(),
            "ch8_out": self.line_ch8_out.text(),
            "ch8_in":  self.line_ch8_in.text(),
        }
        try:
            os.makedirs(_SETTINGS_DIR, exist_ok=True)
            with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[Settings] Failed to save: {e}")

    # --- スピード取得 ---
    def _get_speed(self, ch):
        for btn in self.speed_groups[ch].buttons():
            if btn.isChecked():
                return btn.text()  # "H" / "M" / "L"
        return "M"


    # --- UI状態の切り替え ---
    def toggle_ui_state(self, is_moving):
        for widget in self.interactive_controls:
            widget.setEnabled(not is_moving)


    # --- 移動状態変化ハンドラ ---
    def _on_movement_state_changed(self, is_moving):
        self._is_moving = is_moving
        if not is_moving and self._pending_move is not None:
            # シーケンス継続: UI は無効のまま次のコマンドを実行（ポップアップは維持）
            fn = self._pending_move
            self._pending_move = None
            QTimer.singleShot(100, lambda: self._run_step2(fn))
        else:
            if not is_moving:
                # すべての移動が完了した時点で LOC に戻す
                try:
                    self.controller.switch_to_loc()
                except Exception:
                    print("Not switched to LOC after moves")
                    pass
                # 非アクティブ状態ならポーリングを停止
                if not self.isActiveWindow():
                    self._poller.stop()
                self._close_moving_popup()
                self._shortcut_active = False
                self._last_moved_ch = None
                self._step1_verify_ch = None
                self._update_status_label()
            else:
                self._show_moving_popup()
            self.toggle_ui_state(is_moving)

    def _show_moving_popup(self):
        if self._moving_popup is None:
            self._moving_popup = MovingCameraPopup(self)
            self._moving_popup.show()

    def _close_moving_popup(self):
        if self._moving_popup is not None:
            self._moving_popup.close()
            self._moving_popup = None

    def _update_status_label(self):
        if not hasattr(self, 'lbl_status'):
            return
        ch = self._last_moved_ch
        if ch is not None:
            lines = [tr("Ch{ch} is moving.", ch=ch)]
            if self._shortcut_active:
                lines.append(tr("Shortcut is in operation."))
            self.lbl_status.setText("\n".join(lines))
        else:
            self.lbl_status.setText("")

    def _abort_sequence(self, message):
        """全モーター緊急停止 + ポップアップ閉鎖 + エラーダイアログ"""
        self._step1_verify_ch = None
        self._pending_move = None
        self._shortcut_active = False
        self._last_moved_ch = None
        try:
            self.controller.emergency_stop()
        except Exception:
            pass
        self._close_moving_popup()
        self.toggle_ui_state(False)
        self._is_moving = False
        self._update_status_label()
        QMessageBox.critical(self, tr("Shortcut Error"), message)

    def _run_step2(self, fn):
        """Step1 位置確認 → 必要なら1回リトライ → 第2ステップ実行。
        すでに目的位置の場合は直接クリーンアップする。"""
        if self._step1_verify_ch is not None:
            ch = self._step1_verify_ch
            target = self._step1_verify_target
            try:
                pos_str = self.controller.get_ch_pos(ch)
                current = int(pos_str) if pos_str is not None else None
            except Exception:
                current = None
            if current != target:
                if not self._step1_retry_done:
                    self._step1_retry_done = True
                    self._pending_move = fn
                    try:
                        self.controller.set_ch_speed(ch, self._step1_verify_speed)
                        self.controller.move_ch_absolute(ch, target)
                    except Exception as e:
                        self._abort_sequence(
                            tr("Step1 retry failed for Ch{ch}:\n{error}", ch=ch, error=e)
                        )
                        return
                    QTimer.singleShot(300, self._fire_pending_if_idle)
                    return
                else:
                    current_str = str(current) if current is not None else tr("unknown")
                    self._abort_sequence(
                        tr("Ch{ch} did not reach target {target:+} even after retry.\n"
                           "Current position: {current}.\n"
                           "All motors have been stopped.",
                           ch=ch, target=target, current=current_str)
                    )
                    return
        self._step1_verify_ch = None

        result = fn()
        if result is None:
            # 移動コマンド未送信 → ポーラーが停止を検知できないため直接クリーンアップ
            try:
                self.controller.switch_to_loc()
            except Exception:
                pass
            if not self.isActiveWindow():
                self._poller.stop()
            self._close_moving_popup()
            self.toggle_ui_state(False)
            self._is_moving = False

    def _fire_pending_if_idle(self):
        """ハードウェアに直接問い合わせて停止を確認してから第2ステップを実行する"""
        if self._pending_move is None:
            return
        try:
            still_moving = self.controller.get_is_moving()
        except Exception:
            # 通信エラー → 停止を確認できないためリトライ（停止とみなして進めてはいけない）
            QTimer.singleShot(300, self._fire_pending_if_idle)
            return
        if still_moving:
            QTimer.singleShot(300, self._fire_pending_if_idle)
            return
        fn = self._pending_move
        self._pending_move = None
        self._run_step2(fn)


    # --- 通常停止 ---
    def _on_nstop(self):
        self._pending_move = None
        self._shortcut_active = False
        self._last_moved_ch = None
        self._step1_verify_ch = None
        self.controller.normal_stop()
        self._update_status_label()
        self.toggle_ui_state(False)

    # --- 緊急停止 ---
    def _on_estop(self):
        self._pending_move = None
        self._shortcut_active = False
        self._last_moved_ch = None
        self._step1_verify_ch = None
        self.controller.emergency_stop()
        self._update_status_label()
        self.toggle_ui_state(False)


    # --- ビジュアライゼーション更新 ---
    def update_position_display(self, channel, current_pulse):
        if channel == 9:
            self.viz_view.set_detector_pulse(current_pulse)
        elif channel == 8:
            self.viz_view.set_microscope_pulse(current_pulse)
        if channel in self.lbl_pos:
            self.lbl_pos[channel].setText(f"Ch{channel}: {current_pulse}")

    @staticmethod
    def _parse_speed(response):
        if not response:
            return None
        r = response.upper()
        if r.startswith('H'):
            return 'H'
        if r.startswith('M'):
            return 'M'
        if r.startswith('L'):
            return 'L'
        return None

    # --- 現在位置・速度の即時取得と反映 ---
    def _refresh_positions(self):
        states = self.controller.get_cached_states([6, 7, 8, 9])
        for ch in [6, 7, 8, 9]:
            state = states.get(ch)
            if state is not None:
                pos = state.position
                if ch == 6:
                    self.line_ch6.setText(str(pos))
                elif ch == 7:
                    self.line_ch7.setText(str(pos))
                self.update_position_display(ch, pos)
        if len(states) < 4 and self._initial_refresh_retries < 20:
            # The monitor may still be completing its first 11-channel
            # sweep. Retry from memory only; do not block the GUI on I/O.
            self._initial_refresh_retries += 1
            QTimer.singleShot(250, self._refresh_positions)
            return
        self._initial_refresh_retries = 0
        for ch in [6, 7, 8, 9]:
            try:
                speed = self._parse_speed(self.controller.get_ch_spped(ch))
                if speed and ch in self.speed_groups:
                    for btn in self.speed_groups[ch].buttons():
                        if btn.text() == speed:
                            btn.setChecked(True)
                            break
            except Exception:
                pass

    def _parse_target(self, line_edit) -> "int | None":
        """Parse a move-target QLineEdit's text as an int.

        Returns 0 for a blank field (matching the previous "text() or 0"
        fallback for a deliberately-empty box). On genuinely invalid
        (non-numeric) text, warns the user and returns None instead of
        silently substituting a default target position — an absolute move
        to a fallback of 0 could be an unexpected, potentially unsafe move.
        """
        text = line_edit.text().strip()
        if not text:
            return 0
        try:
            return int(text)
        except ValueError:
            QMessageBox.warning(self, tr("Invalid Value"),
                                 tr("Please enter a valid integer position."))
            return None

    # --- 移動コマンド (共通) ---
    # 戻り値: True=コマンド送信, None=すでに目的位置, False=エラー
    def _move(self, ch, target, speed=None):
        try:
            pos_str = self.controller.get_ch_pos(ch)
            if pos_str is not None and int(pos_str) == target:
                return None  # already at target — do not send command
            self.controller.set_ch_speed(ch, speed if speed is not None else self._get_speed(ch))
            self.controller.move_ch_absolute(ch, target)
            self._last_moved_ch = ch
            self._update_status_label()
            return True
        except ValueError as e:
            # MOVE_CONSTRAINTS による拒否
            QMessageBox.warning(self, tr("Move Blocked"), str(e))
            self._shortcut_active = False
            self._last_moved_ch = None
            self._update_status_label()
            self.toggle_ui_state(False)
            return False
        except Exception as e:
            QMessageBox.critical(self, tr("Controller Error"), str(e))
            self._shortcut_active = False
            self._last_moved_ch = None
            self._update_status_label()
            self.toggle_ui_state(False)
            return False

    def move_det_out(self):
        target = self._parse_target(self.line_det_out)
        if target is None:
            return None
        if target > CH9_CH8_SAFE_BOUNDARY:
            QMessageBox.warning(self, tr("Invalid Value"),
                                tr("Ch9 OUT position must be ≤ {boundary:+}.", boundary=CH9_CH8_SAFE_BOUNDARY))
            return None
        return self._move(9, target)

    def move_det_in(self):
        target = self._parse_target(self.line_det_in)
        return None if target is None else self._move(9, target)

    def move_ch6(self):
        target = self._parse_target(self.line_ch6)
        return None if target is None else self._move(6, target)

    def move_ch7(self):
        target = self._parse_target(self.line_ch7)
        return None if target is None else self._move(7, target)

    def move_ch8_out(self):
        target = self._parse_target(self.line_ch8_out)
        if target is None:
            return None
        if target > 0:
            QMessageBox.warning(self, tr("Invalid Value"), tr("Ch8 OUT position must be ≤ 0."))
            return None
        return self._move(8, target)

    def move_ch8_in(self):
        target = self._parse_target(self.line_ch8_in)
        return None if target is None else self._move(8, target)

    def _start_sequence(self, step2_fn, *, verify_ch=None, verify_target=None, verify_speed="H"):
        """第1ステップ完了後の UI ロック＋ポップアップ＋待機タイマーをまとめて設定する。
        verify_ch/verify_target を指定すると Step1 完了時に位置確認を行い、
        不一致なら1回リトライ、それでも不一致ならエラー停止する。"""
        self._pending_move = step2_fn
        self._step1_verify_ch = verify_ch
        self._step1_verify_target = verify_target
        self._step1_verify_speed = verify_speed
        self._step1_retry_done = False
        self._is_moving = True
        self.toggle_ui_state(True)
        self._show_moving_popup()
        QTimer.singleShot(300, self._fire_pending_if_idle)

    # --- ショートカット ---
    # 制約は常に有効。制約の target_op が '>' なので OUT方向への移動はブロックされない。
    def shortcut_1(self):
        # Det(Ch9)→OUT, その完了後に Mic(Ch8)→IN
        # Ch8 IN (>0) の制約: Ch9 ≤ -30000 が必要。
        # Det OUT を -30000 以下に設定することで step2 の制約が自然に満たされる。
        target = self._parse_target(self.line_det_out)
        if target is None:
            return
        step2_target = self._parse_target(self.line_ch8_in)
        if step2_target is None:
            return
        if target > CH9_CH8_SAFE_BOUNDARY:
            QMessageBox.warning(self, tr("Invalid Value"),
                                tr("Det OUT position must be ≤ {boundary:+}\n"
                                   "(required for Microscope to move IN safely).", boundary=CH9_CH8_SAFE_BOUNDARY))
            return
        self._shortcut_active = True
        step2 = lambda: self._move(8, step2_target, speed="H")
        result = self._move(9, target, speed="H")
        if result is False:
            self._shortcut_active = False
            return
        if result is None:
            # Ch9 すでに OUT 位置 → 位置は確認済みなので step2 を即実行
            self._step1_verify_ch = None
            self._is_moving = True
            self.toggle_ui_state(True)
            self._show_moving_popup()
            self._run_step2(step2)
            return
        self._start_sequence(step2, verify_ch=9, verify_target=target, verify_speed="H")

    def shortcut_2(self):
        # Mic(Ch8)→OUT, その完了後に Det(Ch9)→IN
        # Ch9 IN (>-30000) の制約: Ch8 ≤ 0 が必要。
        # Mic OUT を 0 以下に設定することで step2 の制約が自然に満たされる。
        target = self._parse_target(self.line_ch8_out)
        if target is None:
            return
        step2_target = self._parse_target(self.line_det_in)
        if step2_target is None:
            return
        if target > 0:
            QMessageBox.warning(self, tr("Invalid Value"), tr("Ch8 OUT position must be ≤ 0."))
            return
        self._shortcut_active = True
        step2 = lambda: self._move(9, step2_target, speed="H")
        result = self._move(8, target, speed="H")
        if result is False:
            self._shortcut_active = False
            return
        if result is None:
            # Ch8 すでに OUT 位置 → 位置は確認済みなので step2 を即実行
            self._step1_verify_ch = None
            self._is_moving = True
            self.toggle_ui_state(True)
            self._show_moving_popup()
            self._run_step2(step2)
            return
        self._start_sequence(step2, verify_ch=8, verify_target=target, verify_speed="H")


    def changeEvent(self, event):
        if hasattr(self, '_poller') and event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self._poller.start()
                QTimer.singleShot(0, self._refresh_positions)
            else:
                # 移動中またはシーケンス待機中はポーリングを継続
                if not self._is_moving and self._pending_move is None:
                    self._poller.stop()
        super().changeEvent(event)

    def closeEvent(self, event):
        self._poller.stop()
        if self._owns_controller:
            try:
                self.controller.switch_to_loc()
                self.controller.disconnect()
            except Exception:
                pass
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = Bl18cStageControlApp()
    window.show()
    sys.exit(app.exec())

import logging
import os
import sys
import argparse
import threading
from typing import Callable
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QMessageBox, QGroupBox, QGridLayout, QCheckBox, QLabel, QLineEdit, QDialog,
    QComboBox, QRadioButton, QButtonGroup,
)

# Ensure running from inside bl18c_controller can still import the package
_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)

from utils.stage.control_stage import PM16CController
from utils.stage.control_stage_sim import PM16CControllerSim

from apps.ui_stage_controller.fpd_scope_stg_controller_ui import Bl18cStageControlApp
from apps.stage_simple_all.simple_stage_cont import StageControllerApp
from apps.interactive_camera.interactive_camera import MainWindow as InteractiveCameraWindow
from apps.PACE5000.pace5000_backend import Pace5000Backend
from apps.PACE5000.pace5000_app import Pace5000Window
from apps.LakeShore335.lakeshore335_backend import LakeShore335Backend, DEFAULT_GPIB_ADDRESS
from apps.LakeShore335.lakeshore335_app import LakeShore335Window
from apps.Rad_icon_2022.radicon_backend import RadiconBackend, RadiconBackendSim, RADICON_SERVER, RADICON_DEVICE, RADICON_CCF
from apps.Rad_icon_2022.radicon_ui import RadiconWindow
from apps.dac_scan.dac_scan_app import DacScanWindow
from apps.dac_scan.dac_scan_rot_app import DacScanRotWindow
from apps.dac_scan.collimator_scan_app import CollimatorScanWindow
from apps.scan2d.free_2d_scan_app import Free2DScanWindow
from apps.scan1d.scan1d_app import Scan1DScanWindow
from apps.xrd_scan.xrd_scan_app import XrdScanWindow
from apps.calibrate_instruments.calibrate_instruments_app import CalibrateInstrumentsWindow
try:
    from utils.keithley2000_reader import Keithley2000Reader, KEITHLEY_ADDRESS
    _KEITHLEY_AVAILABLE = True
except ImportError:
    _KEITHLEY_AVAILABLE = False
from apps.ipa_poni.ipa_poni_dialog import IpaPoniDialog
from apps.seq_move.seq_move_app import SeqMoveWindow
from apps.speed_controller.speed_controller_app import SpeedControllerWindow
from apps.dac_oscillation.dac_oscillation_app import DacOscillationWindow
from apps.single_crystal.single_crystal_app import SingleCrystalWindow
from apps.development.keithley_reader.keithley_reader_app import KeithleyReaderWindow
from apps.development.pm16c_console.pm16c_console_app import (
    Pm16cConsoleWindow,
    confirm_pm16c_console_access,
)
from settings.poni_state import PoniState
from settings.settings_window import SettingsWindow
from settings import log_prefs, notification_prefs, i18n
from settings.i18n import tr
import theme


class ModeSelectorLauncher(QMainWindow):
    # (found, tr() template, style color, dynamic detail for the template).
    # The detection thread only probes the device's known GPIB address and
    # reports whether it answered — it never constructs the backend itself,
    # since Keithley2000Reader is constructed on the GUI thread in
    # _on_keithley_result.
    _keithley_result  = pyqtSignal(bool, str, str, str)

    def __init__(self, debug=False, details=False):
        super().__init__()
        self.resize(400, 640)

        log_prefs.load()
        log_prefs.set_details_mode(details)
        notification_prefs.load()
        i18n.load()

        self._debug = debug
        self.controller = None
        self.pace5000_backend = None
        self.lakeshore_backend = None
        self.radicon_backend = None
        self.keithley_reader = None
        self.poni_state = PoniState(self)
        self._settings_window: SettingsWindow | None = None
        self._exp_scheduler_window = None
        self._open_windows: dict[QPushButton | str, QWidget] = {}
        self._stage_conn_state: str | None = None
        self._stage_ok = False
        self._i18n_targets: list[Callable[[], None]] = []

        self._register_tr(lambda: self.setWindowTitle(tr("BL-18C Controller Main")))

        self._keithley_result.connect(self._on_keithley_result)
        self._btn_to_open_fn: dict[QPushButton | str, Callable] = {}

        self._setup_menu_bar()
        self.init_ui()
        self._connect_stage_controller()
        if self._debug:
            self._connect_radicon_sim()
            self._connect_lakeshore_sim()
        QTimer.singleShot(0, self._start_gpib_detection)

        i18n.signals.language_changed.connect(lambda _: self._retranslate_ui())

    # ── i18n helpers ───────────────────────────────────────────────────────

    def _register_tr(self, apply_fn: Callable[[], None]) -> None:
        """Apply *apply_fn* now and re-apply it on every language change."""
        apply_fn()
        self._i18n_targets.append(apply_fn)

    def _retranslate_ui(self) -> None:
        for apply_fn in self._i18n_targets:
            apply_fn()

    def _make_status_setter(self, label: QLabel) -> Callable[..., None]:
        """Return a setter(template, color, **kwargs) for *label* that keeps
        showing tr(template, **kwargs) in *color* across language switches."""
        state: dict = {"template": "", "kwargs": {}, "color": ""}

        def render() -> None:
            text = tr(state["template"], **state["kwargs"]) if state["template"] else ""
            label.setText(text)
            label.setStyleSheet(f"color: {state['color']}; font-weight: bold;" if state["color"] else "")

        def setter(template: str, color: str = "", **kwargs) -> None:
            state["template"] = template
            state["color"] = color
            state["kwargs"] = kwargs
            render()

        self._register_tr(render)
        return setter

    def _connect_stage_controller(self):
        if self._debug:
            ctrl = PM16CControllerSim(debug=True)
            ctrl.connect()
            self.controller = ctrl
            self._stage_ok = True
            self._set_stage_status("● Simulation", "orange")
            return
        try:
            ctrl = PM16CController(ip='192.168.1.55', port=7777, debug=True)
            ctrl.connect()
            self.controller = ctrl
            self._stage_ok = True
            self._set_stage_status("● Connected", "green")
        except Exception as e:
            self._set_stage_status("✕ Failed", "red")
            QMessageBox.critical(
                self, tr("Connection Error"),
                tr("Could not connect to stage controller:\n{error}\n\n"
                   "Sub-applications will not be able to control the stage.", error=e),
            )
            self._disable_stage_dependent_ui()

    def _disable_stage_dependent_ui(self) -> None:
        """Grey out buttons/menu actions that require a working stage connection.

        There is no in-app "reconnect stage" control, so once the startup
        connection attempt fails these stay disabled until the app restarts.
        """
        for btn in (
            self.btn_dac_fpd_stage, self.btn_interactive_camera,
            self.btn_simple_stage_cont, self.btn_dac_oscillation,
            self.btn_collimator_scan, self.btn_dac_scan, self.btn_dac_scan_rot,
            self.btn_scan1d, self.btn_free_2d_scan, self.btn_xrd_scan,
        ):
            btn.setEnabled(False)
        for action in (
            self._single_crystal_action, self._seq_move_action,
            self._speed_controller_action, self._pm16c_console_action,
        ):
            action.setEnabled(False)

    def _connect_lakeshore_sim(self):
        backend = LakeShore335Backend(simulate=True)
        backend.connect()
        self.lakeshore_backend = backend
        self.lakeshore_cb.setChecked(True)
        self._set_lakeshore_status("● Simulation", "orange")
        self.btn_lakeshore.setEnabled(True)

    def _connect_radicon_sim(self):
        backend = RadiconBackendSim()
        self.radicon_backend = backend
        self.radicon_cb.setChecked(True)
        self.radicon_bin_combo.setEnabled(False)
        self._set_radicon_status(
            "● Simulation  {width} × {height} px", "orange",
            width=backend.width, height=backend.height,
        )
        self.btn_radicon.setEnabled(True)
        self.btn_xrd_scan.setEnabled(True)
        self.btn_calibrate_instruments.setEnabled(True)

    def _setup_menu_bar(self):
        menu_bar = self.menuBar()

        settings_menu = menu_bar.addMenu(tr("Settings"))
        self._register_tr(lambda: settings_menu.setTitle(tr("Settings")))
        settings_action = settings_menu.addAction(tr("Settings…"))
        self._register_tr(lambda: settings_action.setText(tr("Settings…")))
        settings_action.triggered.connect(self._on_open_settings)

        tools_menu = menu_bar.addMenu(tr("Tools"))
        self._register_tr(lambda: tools_menu.setTitle(tr("Tools")))

        ruby_finder_action = tools_menu.addAction(tr("Ruby Finder"))
        self._register_tr(lambda: ruby_finder_action.setText(tr("Ruby Finder")))
        ruby_finder_action.setEnabled(False)

        tools_menu.addSeparator()

        self._single_crystal_action = tools_menu.addAction(tr("Single crystal measurements"))
        self._register_tr(lambda: self._single_crystal_action.setText(tr("Single crystal measurements")))
        self._single_crystal_action.triggered.connect(self._on_single_crystal)

        tools_menu.addSeparator()

        self._seq_move_action = tools_menu.addAction(tr("Sequential Relative Moves"))
        self._register_tr(lambda: self._seq_move_action.setText(tr("Sequential Relative Moves")))
        self._seq_move_action.triggered.connect(self._on_seq_move)

        self._speed_controller_action = tools_menu.addAction(tr("Speed Controller"))
        self._register_tr(lambda: self._speed_controller_action.setText(tr("Speed Controller")))
        self._speed_controller_action.triggered.connect(self._on_speed_controller)

        tools_menu.addSeparator()

        convert_action = tools_menu.addAction(tr("Convert IPA prm file to poni format"))
        self._register_tr(lambda: convert_action.setText(tr("Convert IPA prm file to poni format")))
        convert_action.triggered.connect(self._on_convert_ipa_prm)

        # Development menu — English-only, not translated (see CLAUDE.md).
        development_menu = menu_bar.addMenu("Development")

        keithley_reader_action = development_menu.addAction("Keithley Reader")
        keithley_reader_action.triggered.connect(self._on_keithley_reader)

        self._pm16c_console_action = development_menu.addAction("PM16C Console")
        self._pm16c_console_action.triggered.connect(self._on_pm16c_console)

    def _on_pm16c_console(self) -> None:
        if 'pm16c_console' not in self._open_windows:
            if not confirm_pm16c_console_access(self):
                return
        self._launch_window(
            'pm16c_console',
            lambda: Pm16cConsoleWindow(controller=self.controller),
        )

    def _on_keithley_reader(self) -> None:
        if self.keithley_reader is None:
            QMessageBox.warning(
                self, tr("Keithley 2000 Not Connected"),
                tr("To open Keithley Reader,\n"
                   "please connect Keithley 2000 first (Hardware Connections checkbox)."),
            )
            return
        self._launch_window(
            'keithley_reader',
            lambda: KeithleyReaderWindow(reader=self.keithley_reader),
        )

    def _on_single_crystal(self) -> None:
        if self.radicon_backend is None:
            QMessageBox.warning(
                self, tr("Rad-icon 2022 Not Connected"),
                tr("To open Single crystal measurements,\n"
                   "please connect Rad-icon 2022 first (Hardware Connections checkbox)."),
            )
            return
        if self.controller is None:
            QMessageBox.warning(
                self, tr("Stage Not Connected"),
                tr("To open Single crystal measurements,\n"
                   "a connection to the stage controller is required."),
            )
            return
        self._launch_window(
            'single_crystal',
            lambda: SingleCrystalWindow(
                backend=self.radicon_backend,
                controller=self.controller,
            ),
        )

    def _on_seq_move(self) -> None:
        self._launch_window(
            'seq_move',
            lambda: SeqMoveWindow(controller=self.controller),
        )

    def _on_speed_controller(self) -> None:
        self._launch_window(
            'speed_controller',
            lambda: SpeedControllerWindow(controller=self.controller),
        )

    def _on_open_settings(self) -> None:
        if self._settings_window is None:
            self._settings_window = SettingsWindow(
                poni_state=self.poni_state,
                open_calibrate_instruments=self.open_calibrate_instruments,
                parent=self,
            )
            self._settings_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._settings_window.destroyed.connect(
                lambda: setattr(self, '_settings_window', None)
            )
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()

    def _on_convert_ipa_prm(self):
        dlg = IpaPoniDialog(self)
        dlg.exec()

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # ── Hardware connection checklist ──────────────────────────────────
        hw_group = QGroupBox()
        self._register_tr(lambda: hw_group.setTitle(tr("Hardware Connections")))
        hw_grid = QGridLayout()
        hw_grid.setColumnStretch(1, 1)

        # Row 0: Stage Controller (always connected, checkbox disabled)
        stage_cb = QCheckBox()
        stage_cb.setChecked(True)
        stage_cb.setEnabled(False)
        hw_grid.addWidget(stage_cb, 0, 0)
        stage_name_lbl = QLabel()
        self._register_tr(lambda: stage_name_lbl.setText(tr("Stage Controller  192.168.1.55:7777")))
        hw_grid.addWidget(stage_name_lbl, 0, 1)
        self.stage_status_label = QLabel()
        self._set_stage_status = self._make_status_setter(self.stage_status_label)
        self._set_stage_status("Connecting…", "gray")
        hw_grid.addWidget(self.stage_status_label, 0, 2)

        # Row 1: PACE5000 (optional)
        self.pace5000_cb = QCheckBox()
        self.pace5000_cb.clicked.connect(self._on_pace5000_toggled)
        hw_grid.addWidget(self.pace5000_cb, 1, 0)
        pace5000_name_lbl = QLabel()
        self._register_tr(lambda: pace5000_name_lbl.setText(tr("PACE5000")))
        hw_grid.addWidget(pace5000_name_lbl, 1, 1)
        pace5000_right = QWidget()
        pace5000_right_layout = QGridLayout(pace5000_right)
        pace5000_right_layout.setContentsMargins(0, 0, 0, 0)
        self.pace5000_ip_input = QLineEdit("192.168.1.104")
        self.pace5000_ip_input.setMinimumWidth(200)
        self.pace5000_ip_input.setEnabled(True)  # Always enabled since IP is needed for connection
        self.pace5000_status_label = QLabel()
        self._set_pace5000_status = self._make_status_setter(self.pace5000_status_label)
        self._set_pace5000_status("", "")
        pace5000_right_layout.addWidget(self.pace5000_ip_input, 0, 0)
        pace5000_right_layout.addWidget(self.pace5000_status_label, 0, 1)
        hw_grid.addWidget(pace5000_right, 1, 2)

        # Row 2: LakeShore 335 (optional, default off)
        self.lakeshore_cb = QCheckBox()
        self.lakeshore_cb.clicked.connect(self._on_lakeshore_toggled)
        hw_grid.addWidget(self.lakeshore_cb, 2, 0)
        lakeshore_name_lbl = QLabel()
        self._register_tr(lambda: lakeshore_name_lbl.setText(tr("LakeShore 335  (GPIB)")))
        hw_grid.addWidget(lakeshore_name_lbl, 2, 1)
        self.lakeshore_status_label = QLabel()
        self._set_lakeshore_status = self._make_status_setter(self.lakeshore_status_label)
        self._set_lakeshore_status("", "")
        hw_grid.addWidget(self.lakeshore_status_label, 2, 2)

        # Row 3: Keithley 2000 GPIB (optional)
        self.keithley_cb = QCheckBox()
        self.keithley_cb.stateChanged.connect(self._on_keithley_toggled)
        hw_grid.addWidget(self.keithley_cb, 3, 0)
        keithley_name_lbl = QLabel()
        self._register_tr(lambda: keithley_name_lbl.setText(tr("Keithley 2000  GPIB0::2")))
        hw_grid.addWidget(keithley_name_lbl, 3, 1)
        self.keithley_status_label = QLabel()
        self._set_keithley_status = self._make_status_setter(self.keithley_status_label)
        self._set_keithley_status("", "")
        hw_grid.addWidget(self.keithley_status_label, 3, 2)

        # Row 4: Rad-icon 2022 (optional, default off)
        self.radicon_cb = QCheckBox()
        self.radicon_cb.clicked.connect(self._on_radicon_toggled)
        hw_grid.addWidget(self.radicon_cb, 4, 0)
        radicon_name_lbl = QLabel()
        self._register_tr(lambda: radicon_name_lbl.setText(tr("Rad-icon 2022 FPD Controller")))
        hw_grid.addWidget(radicon_name_lbl, 4, 1)
        radicon_right = QWidget()
        radicon_right_layout = QGridLayout(radicon_right)
        radicon_right_layout.setContentsMargins(0, 0, 0, 0)
        self.radicon_bin_combo = QComboBox()
        self.radicon_bin_combo.addItem(tr("2*2  (1040 * 1118 px)"), "2x2")
        self.radicon_bin_combo.addItem(tr("None  (2080 * 2238 px)"), "1x1")
        self._register_tr(lambda: self.radicon_bin_combo.setItemText(0, tr("2*2  (1040 * 1118 px)")))
        self._register_tr(lambda: self.radicon_bin_combo.setItemText(1, tr("None  (2080 * 2238 px)")))
        self.radicon_status_label = QLabel()
        self._set_radicon_status = self._make_status_setter(self.radicon_status_label)
        self._set_radicon_status("", "")
        radicon_right_layout.addWidget(self.radicon_bin_combo, 0, 0)
        radicon_right_layout.addWidget(self.radicon_status_label, 0, 1)
        hw_grid.addWidget(radicon_right, 4, 2)

        hw_group.setLayout(hw_grid)
        layout.addWidget(hw_group)

        # ── App launch buttons ─────────────────────────────────────────────
        sections = [
            ("Stage Control", [
                ("Microscope + FPD stage control",   self.open_dac_fpd_stage,      True,  "btn_dac_fpd_stage"),
                ("Interactive camera",               self.open_interactive_camera, True,  "btn_interactive_camera"),
                ("Simple controller for all stages", self.open_simple_stage_cont,  True,  "btn_simple_stage_cont"),
                ("DAC stage oscillation",            self.open_dac_oscillation,    True,  "btn_dac_oscillation"),
            ]),
            ("Scan", [
                ("Collimator Scan",            self.open_collimator_scan, True,  "btn_collimator_scan"),
                ("DAC Scan (Normal)",          self.open_dac_scan,        True,  "btn_dac_scan"),
                ("DAC Scan (Rotation Centre)", self.open_dac_scan_rot,    True,  "btn_dac_scan_rot"),
                ("DAC Scan (XRD)",             self.open_xrd_scan,        False, "btn_xrd_scan"),
                ("General 1D Scan",                    self.open_scan1d,          True,  "btn_scan1d"),
                ("General 2D Scan",               self.open_free_2d_scan,    True,  "btn_free_2d_scan"),
            ]),
            ("XRD", [
                ("Rad-icon 2022 (FPD) Controller", self.open_radicon, False, "btn_radicon"),
                ("Calibrate Detector Geometry", self.open_calibrate_instruments, False, "btn_calibrate_instruments"),
            ]),
            ("Sample Environment", [
                ("PACE5000",      self.open_pace5000,  False, "btn_pace5000"),
                ("LakeShore 335", self.open_lakeshore, False, "btn_lakeshore"),
            ]),
            ("Automation", [
                ("Experimental Scheduler", self.open_exp_scheduler, True, "btn_exp_scheduler"),
            ]),
        ]

        for section_name, buttons in sections:
            group = QGroupBox()
            self._register_tr(lambda group=group, name=section_name: group.setTitle(tr(name)))
            group_layout = QVBoxLayout()
            group_layout.setContentsMargins(8, 6, 8, 8)
            group_layout.setSpacing(0)
            for i, (label, slot, enabled, attr) in enumerate(buttons):
                btn = QPushButton()
                self._register_tr(lambda btn=btn, label=label: btn.setText(tr(label)))
                btn.setProperty("launcher", True)
                if i == 0:
                    btn.setProperty("list_first", True)
                if i == len(buttons) - 1:
                    btn.setProperty("list_last", True)
                btn.setEnabled(enabled)
                if slot:
                    btn.clicked.connect(slot)
                if attr:
                    setattr(self, attr, btn)
                group_layout.addWidget(btn)
            group.setLayout(group_layout)
            layout.addWidget(group)

        # ── Language ─────────────────────────────────────────────────────
        lang_group = QGroupBox()
        self._register_tr(lambda: lang_group.setTitle(tr("Language")))
        lang_row = QHBoxLayout()
        self.lang_en_radio = QRadioButton("English")
        self.lang_ja_radio = QRadioButton("日本語")
        lang_btn_group = QButtonGroup(self)
        lang_btn_group.addButton(self.lang_en_radio)
        lang_btn_group.addButton(self.lang_ja_radio)
        (self.lang_ja_radio if i18n.get_language() == "ja" else self.lang_en_radio).setChecked(True)
        self.lang_en_radio.toggled.connect(lambda checked: checked and i18n.set_language("en"))
        self.lang_ja_radio.toggled.connect(lambda checked: checked and i18n.set_language("ja"))
        lang_row.addWidget(self.lang_en_radio)
        lang_row.addWidget(self.lang_ja_radio)
        lang_row.addStretch()
        lang_group.setLayout(lang_row)
        layout.addWidget(lang_group)

        # Map keys to their open-functions (used by close/restore_sub_windows).
        # btn_exp_scheduler is intentionally excluded — the scheduler opens itself.
        self._btn_to_open_fn = {
            self.btn_dac_fpd_stage:      self.open_dac_fpd_stage,
            self.btn_interactive_camera: self.open_interactive_camera,
            self.btn_simple_stage_cont:  self.open_simple_stage_cont,
            self.btn_dac_oscillation:    self.open_dac_oscillation,
            self.btn_collimator_scan:    self.open_collimator_scan,
            self.btn_dac_scan:           self.open_dac_scan,
            self.btn_dac_scan_rot:       self.open_dac_scan_rot,
            self.btn_xrd_scan:           self.open_xrd_scan,
            self.btn_pace5000:           self.open_pace5000,
            self.btn_lakeshore:          self.open_lakeshore,
            self.btn_radicon:            self.open_radicon,
            self.btn_calibrate_instruments: self.open_calibrate_instruments,
            self.btn_scan1d:             self.open_scan1d,
            self.btn_free_2d_scan:       self.open_free_2d_scan,
            'single_crystal':            self._on_single_crystal,
            'seq_move':                  self._on_seq_move,
            'speed_controller':          self._on_speed_controller,
        }

    # ── Hardware toggle handlers ───────────────────────────────────────────

    def _on_pace5000_toggled(self, checked: bool):
        if checked:
            ip = self.pace5000_ip_input.text().strip()
            self._set_pace5000_status("Connecting…", "gray")
            wait = self._show_wait_dialog()
            try:
                backend = Pace5000Backend(connection="tcp", ip_address=ip)
                backend.connect_device()
            except Exception as e:
                wait.close()
                self._set_pace5000_status("✕ Failed", "red")
                QMessageBox.critical(self, tr("PACE5000 Connection Error"),
                                     tr("Could not connect to PACE5000:\n{error}", error=e))
                self.pace5000_cb.setChecked(False)
                self.btn_pace5000.setEnabled(False)
                return
            wait.close()
            if backend.connected:
                self.pace5000_backend = backend
                self._set_pace5000_status("● Connected", "green")
                self.btn_pace5000.setEnabled(True)
            else:
                self._set_pace5000_status("✕ Failed", "red")
                self.pace5000_cb.setChecked(False)
                self.btn_pace5000.setEnabled(False)
        else:
            if self.pace5000_backend is not None:
                try:
                    self.pace5000_backend.stop()
                except Exception:
                    pass
                self.pace5000_backend = None
            self._set_pace5000_status("", "")
            self.btn_pace5000.setEnabled(False)

    def _start_gpib_detection(self) -> None:
        threading.Thread(target=self._detect_gpib_devices, daemon=True).start()

    def _detect_gpib_devices(self) -> None:
        if self._debug:
            return

        try:
            import pyvisa
        except ImportError:
            return

        # ── Keithley 2000 — probe only its known GPIB address. Sending
        # *IDN? to every resource on the bus risks confusing or disturbing
        # unrelated GPIB instruments; querying just the one address this
        # device is expected at avoids that, and also avoids treating any
        # other instrument's numeric-looking response as a Talk-Only Keithley.
        if not _KEITHLEY_AVAILABLE:
            return
        keithley_found = False
        try:
            rm = pyvisa.ResourceManager()
            try:
                instr = rm.open_resource(KEITHLEY_ADDRESS)
                try:
                    instr.timeout = 2000
                    instr.query("*IDN?")
                    keithley_found = True
                finally:
                    instr.close()
            finally:
                rm.close()
        except Exception:
            pass
        if keithley_found:
            self._keithley_result.emit(True, "", "", KEITHLEY_ADDRESS)

    def _on_lakeshore_toggled(self, checked: bool):
        if checked:
            try:
                import pyvisa
            except ImportError:
                QMessageBox.critical(self, tr("LakeShore 335 Error"),
                                     tr("pyvisa is not installed.\nRun: pip install pyvisa"))
                self.lakeshore_cb.setChecked(False)
                return
            self._set_lakeshore_status("Connecting…", "gray")
            wait = self._show_wait_dialog()
            try:
                # Probe only the known GPIB address first — sending *IDN? to
                # every resource on the bus risks confusing or disturbing
                # unrelated GPIB instruments.
                rm = pyvisa.ResourceManager()
                try:
                    instr = rm.open_resource(DEFAULT_GPIB_ADDRESS)
                    try:
                        instr.timeout = 2000
                        idn = instr.query("*IDN?").strip()
                        found = "LSCI" in idn and "MODEL335" in idn
                    finally:
                        instr.close()
                finally:
                    rm.close()
                if not found:
                    raise RuntimeError(tr("No LakeShore 335 found at {address}", address=DEFAULT_GPIB_ADDRESS))
                backend = LakeShore335Backend(simulate=False)
                backend.connect(gpib_address=DEFAULT_GPIB_ADDRESS)
            except Exception as e:
                wait.close()
                self._set_lakeshore_status("✕ Failed", "red")
                QMessageBox.critical(self, tr("LakeShore 335 Connection Error"),
                                     tr("Could not connect to LakeShore 335:\n{error}", error=e))
                self.lakeshore_cb.setChecked(False)
                self.btn_lakeshore.setEnabled(False)
                return
            wait.close()
            self.lakeshore_backend = backend
            self._set_lakeshore_status("● Connected  {detail}", "green", detail=DEFAULT_GPIB_ADDRESS)
            self.btn_lakeshore.setEnabled(True)
        else:
            if self.lakeshore_backend is not None:
                try:
                    self.lakeshore_backend.disconnect()
                except Exception:
                    pass
                self.lakeshore_backend = None
            self._set_lakeshore_status("", "")
            self.btn_lakeshore.setEnabled(False)

    @pyqtSlot(bool, str, str, str)
    def _on_keithley_result(self, found: bool, template: str, color: str, detail: str) -> None:
        if not found:
            return
        try:
            reader = Keithley2000Reader(address=detail)
        except Exception:
            return
        self.keithley_reader = reader
        self.keithley_cb.blockSignals(True)
        self.keithley_cb.setChecked(True)
        self.keithley_cb.blockSignals(False)
        if reader.is_talk_only:
            self._set_keithley_status("● Connected  (Talk-Only)  {detail}", "orange", detail=detail)
        else:
            self._set_keithley_status("● Connected  {detail}", "green", detail=detail)

    def _on_keithley_toggled(self, __state):
        checked = self.keithley_cb.isChecked()
        if checked:
            if not _KEITHLEY_AVAILABLE:
                QMessageBox.critical(self, tr("Keithley 2000 Error"),
                                     tr("pyvisa is not installed.\nRun: pip install pyvisa"))
                self.keithley_cb.blockSignals(True)
                self.keithley_cb.setChecked(False)
                self.keithley_cb.blockSignals(False)
                return
            self._set_keithley_status("Connecting…", "gray")
            wait = self._show_wait_dialog()
            try:
                reader = Keithley2000Reader()
            except Exception as e:
                wait.close()
                self._set_keithley_status("✕ Failed", "red")
                QMessageBox.critical(self, tr("Keithley 2000 Connection Error"),
                                     tr("Could not connect to Keithley 2000:\n{error}", error=e))
                self.keithley_cb.blockSignals(True)
                self.keithley_cb.setChecked(False)
                self.keithley_cb.blockSignals(False)
                return
            wait.close()
            self.keithley_reader = reader
            if reader.is_talk_only:
                self._set_keithley_status("● Connected  (Talk-Only)", "orange")
            else:
                self._set_keithley_status("● Connected", "green")
        else:
            if self.keithley_reader is not None:
                try:
                    self.keithley_reader.close()
                except Exception:
                    pass
                self.keithley_reader = None
            self._set_keithley_status("", "")

    def _on_radicon_toggled(self, checked: bool):
        if checked:
            binning = self.radicon_bin_combo.currentData()
            ccf_path = RADICON_CCF[binning]
            self._set_radicon_status("Connecting…", "gray")
            self.radicon_bin_combo.setEnabled(False)
            wait = self._show_wait_dialog()
            try:
                backend = RadiconBackend(RADICON_SERVER, RADICON_DEVICE, ccf_path)
            except Exception as e:
                wait.close()
                self._set_radicon_status("✕ Failed", "red")
                self.radicon_bin_combo.setEnabled(True)
                QMessageBox.critical(self, tr("Rad-icon 2022 Connection Error"),
                                     tr("Could not connect to Rad-icon 2022:\n{error}", error=e))
                self.radicon_cb.setChecked(False)
                self.btn_radicon.setEnabled(False)
                return
            wait.close()
            self.radicon_backend = backend
            self._set_radicon_status(
                "● Connected  {width} × {height} px", "green",
                width=backend.width, height=backend.height,
            )
            self.btn_radicon.setEnabled(True)
            self.btn_xrd_scan.setEnabled(self._stage_ok)
            self.btn_calibrate_instruments.setEnabled(True)
        else:
            if self.radicon_backend is not None:
                try:
                    self.radicon_backend.close()
                except Exception:
                    pass
                self.radicon_backend = None
            self._set_radicon_status("", "")
            self.radicon_bin_combo.setEnabled(True)
            self.btn_radicon.setEnabled(False)
            self.btn_xrd_scan.setEnabled(False)
            self.btn_calibrate_instruments.setEnabled(False)

    # ── Close handler ─────────────────────────────────────────────────────

    def closeEvent(self, event):
        reply = QMessageBox.question(
            self, tr('Confirm Exit'),
            tr("Are you sure to close all the windows and exit the controller program?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.Yes:
            if self.pace5000_backend is not None:
                try:
                    self.pace5000_backend.stop()
                except Exception:
                    pass
            if self.lakeshore_backend is not None:
                try:
                    self.lakeshore_backend.disconnect()
                except Exception:
                    pass
            if self.radicon_backend is not None:
                try:
                    self.radicon_backend.close()
                except Exception:
                    pass
            if self.keithley_reader is not None:
                try:
                    self.keithley_reader.close()
                except Exception:
                    pass
            if self.controller:
                try:
                    # shutdown() = best-effort LOC (no lease needed at app
                    # exit) + disconnect; switch_to_loc() now requires a
                    # MotionLease.
                    self.controller.shutdown()
                except Exception:
                    pass
            event.accept()
            QApplication.instance().quit()
        else:
            event.ignore()

    # ── Window openers ────────────────────────────────────────────────────

    def _show_wait_dialog(self) -> QDialog:
        dlg = QDialog(self, Qt.WindowType.FramelessWindowHint)
        dlg.setStyleSheet("background-color: #2196F3;")
        layout = QVBoxLayout(dlg)
        lbl = QLabel(tr("Please wait..."))
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("font-size: 24px; padding: 30px 50px; color: white; background-color: transparent;")
        layout.addWidget(lbl)
        dlg.adjustSize()
        center = self.frameGeometry().center()
        dlg.move(center.x() - dlg.width() // 2, center.y() - dlg.height() // 2)
        dlg.show()
        QApplication.processEvents()
        return dlg

    def _launch_window(self, key: QPushButton | str, factory) -> None:
        if key in self._open_windows:
            self._open_windows[key].raise_()
            self._open_windows[key].activateWindow()
            return
        wait = self._show_wait_dialog()
        try:
            window = factory()
        except Exception as e:
            wait.close()
            QMessageBox.critical(
                self, tr("Could Not Open Window"),
                tr("Failed to open the window:\n{error}", error=e),
            )
            return
        wait.close()
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        window.destroyed.connect(lambda: self._on_window_closed(key))
        self._open_windows[key] = window
        if isinstance(key, QPushButton):
            self._set_btn_active(key, True)
        window.show()

    @staticmethod
    def _set_btn_active(btn: QPushButton, active: bool) -> None:
        btn.setProperty("active", active)
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _on_window_closed(self, key: QPushButton | str) -> None:
        self._open_windows.pop(key, None)
        if isinstance(key, QPushButton):
            self._set_btn_active(key, False)

    def open_dac_fpd_stage(self):
        self._launch_window(self.btn_dac_fpd_stage,
                            lambda: Bl18cStageControlApp(controller=self.controller))

    def open_interactive_camera(self):
        self._launch_window(self.btn_interactive_camera,
                            lambda: InteractiveCameraWindow(controller=self.controller))

    def open_simple_stage_cont(self):
        self._launch_window(self.btn_simple_stage_cont,
                            lambda: StageControllerApp(controller=self.controller))

    def open_dac_oscillation(self):
        self._launch_window(self.btn_dac_oscillation,
                            lambda: DacOscillationWindow(controller=self.controller))

    def open_collimator_scan(self):
        reader = self.keithley_reader
        self._launch_window(
            self.btn_collimator_scan,
            lambda: CollimatorScanWindow(
                controller=self.controller,
                gpib_reader=reader,
                debug=self._debug,
            ),
        )

    def open_dac_scan(self):
        reader = self.keithley_reader
        self._launch_window(
            self.btn_dac_scan,
            lambda: DacScanWindow(
                controller=self.controller,
                gpib_reader=reader,
                debug=self._debug,
            ),
        )

    def open_dac_scan_rot(self):
        reader = self.keithley_reader
        self._launch_window(
            self.btn_dac_scan_rot,
            lambda: DacScanRotWindow(
                controller=self.controller,
                gpib_reader=reader,
                debug=self._debug,
            ),
        )

    def open_scan1d(self):
        reader = self.keithley_reader
        self._launch_window(
            self.btn_scan1d,
            lambda: Scan1DScanWindow(
                controller=self.controller,
                gpib_reader=reader,
                debug=self._debug,
            ),
        )

    def open_free_2d_scan(self):
        reader = self.keithley_reader
        self._launch_window(
            self.btn_free_2d_scan,
            lambda: Free2DScanWindow(
                controller=self.controller,
                gpib_reader=reader,
                debug=self._debug,
            ),
        )

    def open_pace5000(self):
        self._launch_window(self.btn_pace5000,
                            lambda: Pace5000Window(backend=self.pace5000_backend))

    def open_lakeshore(self):
        self._launch_window(self.btn_lakeshore,
                            lambda: LakeShore335Window(backend=self.lakeshore_backend))

    def open_radicon(self):
        self._launch_window(
            self.btn_radicon,
            lambda: RadiconWindow(
                backend=self.radicon_backend,
                poni_state=self.poni_state,
                controller=self.controller,
            ),
        )

    def open_xrd_scan(self):
        self._launch_window(
            self.btn_xrd_scan,
            lambda: XrdScanWindow(
                controller=self.controller,
                backend=self.radicon_backend,
                poni_state=self.poni_state,
            ),
        )

    def open_calibrate_instruments(self):
        self._launch_window(
            self.btn_calibrate_instruments,
            lambda: CalibrateInstrumentsWindow(
                controller=self.controller,
                backend=self.radicon_backend,
                poni_state=self.poni_state,
                get_radicon_window=lambda: self._open_windows.get(self.btn_radicon),
            ),
        )

    def open_exp_scheduler(self):
        if self._exp_scheduler_window is not None:
            self._exp_scheduler_window.raise_()
            self._exp_scheduler_window.activateWindow()
            return
        from apps.exp_scheduler.device_context import DeviceContext
        from apps.exp_scheduler.ui.scheduler_window import ExperimentalSchedulerWindow
        ctx = DeviceContext(
            controller=self.controller,
            pace5000=self.pace5000_backend,
            lakeshore=self.lakeshore_backend,
            radicon=self.radicon_backend,
        )
        wait = self._show_wait_dialog()
        try:
            window = ExperimentalSchedulerWindow(ctx=ctx, main_window=self)
        except Exception as e:
            wait.close()
            QMessageBox.critical(
                self, tr("Could Not Open Window"),
                tr("Failed to open the window:\n{error}", error=e),
            )
            return
        wait.close()
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        window.destroyed.connect(lambda: setattr(self, '_exp_scheduler_window', None))
        window.destroyed.connect(lambda: self._set_btn_active(self.btn_exp_scheduler, False))
        self._exp_scheduler_window = window
        self._set_btn_active(self.btn_exp_scheduler, True)
        window.show()

    def close_all_sub_windows(self) -> list:
        """Close all open sub-windows and return their keys for later restoration."""
        open_keys = list(self._open_windows.keys())
        for window in list(self._open_windows.values()):
            window.close()
        return open_keys

    def restore_sub_windows(self, keys: list) -> None:
        """Re-open the sub-windows that were open before close_all_sub_windows()."""
        for key in keys:
            fn = self._btn_to_open_fn.get(key)
            if fn:
                fn()


def list_gpib_devices() -> None:
    try:
        import pyvisa
    except ImportError:
        print("[GPIB] pyvisa not installed — skipping device scan")
        return

    try:
        rm = pyvisa.ResourceManager()
    except Exception as e:
        print(f"[GPIB] ResourceManager init failed: {e}")
        return

    resources = rm.list_resources()
    gpib = [r for r in resources if "GPIB" in r.upper() and "INTFC" not in r.upper()]

    if not gpib:
        print("[GPIB] No GPIB devices found")
        rm.close()
        return

    print(f"[GPIB] {len(gpib)} device(s) found:")
    for addr in gpib:
        try:
            instr = rm.open_resource(addr)
            idn = instr.query("*IDN?").strip()
            instr.close()
            print(f"  {addr}  →  {idn}")
        except Exception as e:
            print(f"  {addr}  →  (IDN query failed: {e})")

    rm.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    list_gpib_devices()

    parser = argparse.ArgumentParser(description="BL-18C Controller")
    parser.add_argument("--debug", action="store_true",
                        help="Run with simulated hardware (no real controller needed)")
    parser.add_argument("--details", action="store_true",
                        help="Enable detailed logging (e.g., save autofocus sharpness data as CSV)")
    args, qt_args = parser.parse_known_args()

    app = QApplication([sys.argv[0]] + qt_args)
    theme.apply(app)
    launcher = ModeSelectorLauncher(debug=args.debug, details=args.details)
    launcher.show()
    sys.exit(app.exec())

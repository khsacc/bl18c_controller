import sys
import socket
import time
from PyQt6 import QtCore, QtGui, QtWidgets
from utils.stage.control_stage import PM16CController
from settings.i18n import tr


def _no_wheel(widget):
    """Ignore mouse-wheel events on spin boxes so scrolling the panel never
    silently changes a value the cursor happens to be hovering over."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


# Overrides theme.py's global QPushButton padding/margin (10px/16px, 2px 0),
# which otherwise dominates each row's height regardless of layout spacing.
_COMPACT_BTN_STYLE = "QPushButton { padding: 4px 10px; margin: 0px; }"

# Overrides theme.py's global QSpinBox padding/min-height (6px 8px / 28px),
# which otherwise keeps each row tall regardless of layout spacing.
_COMPACT_SPINBOX_STYLE = "QSpinBox { padding: 2px 6px; min-height: 18px; }"



class MotorControlWidget(QtWidgets.QWidget):
    """Widget for controlling a single motor channel"""
    
    def __init__(self, ch_number, controller, parent=None):
        super().__init__(parent)
        self.ch_number = ch_number
        self.controller = controller
        self.current_speed = "M"
        
        # Get hardware limits
        try:
            bl = self.controller.read_backward_limit(ch_number)
            fl = self.controller.read_forward_limit(ch_number)
            self.backward_limit = int(bl) if bl else -999999
            self.forward_limit = int(fl) if fl else 999999
        except:
            self.backward_limit = -999999
            self.forward_limit = 999999
        
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)

        # Channel label
        ch_label = QtWidgets.QLabel(tr("Ch{ch}:", ch=ch_number))
        ch_label.setMinimumWidth(50)
        layout.addWidget(ch_label)

        # Current position display
        self.pos_label = QtWidgets.QLabel(tr("--reading--"))
        self.pos_label.setMinimumWidth(100)
        layout.addWidget(self.pos_label)

        # Target position input
        self.target_input = _no_wheel(QtWidgets.QSpinBox())
        self.target_input.setRange(self.backward_limit, self.forward_limit)
        self.target_input.setValue(0)
        self.target_input.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.target_input.setStyleSheet(_COMPACT_SPINBOX_STYLE)
        self.target_input.setFixedWidth(90)
        layout.addWidget(self.target_input)

        # Move to absolute position button
        move_abs_btn = QtWidgets.QPushButton(tr("Move Abs"))
        move_abs_btn.setStyleSheet(_COMPACT_BTN_STYLE)
        move_abs_btn.clicked.connect(self.move_absolute)
        layout.addWidget(move_abs_btn)

        # Relative movement controls
        self.rel_step_input = _no_wheel(QtWidgets.QSpinBox())
        self.rel_step_input.setRange(1, 99999)
        self.rel_step_input.setValue(10)
        self.rel_step_input.setFixedWidth(70)
        self.rel_step_input.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.rel_step_input.setStyleSheet(_COMPACT_SPINBOX_STYLE)

        rel_layout = QtWidgets.QHBoxLayout()
        rel_minus_btn = QtWidgets.QPushButton(tr("Relative -"))
        rel_minus_btn.setStyleSheet(_COMPACT_BTN_STYLE)
        rel_minus_btn.clicked.connect(lambda: self.move_relative(-self.rel_step_input.value()))
        rel_layout.addWidget(rel_minus_btn)
        rel_layout.addWidget(self.rel_step_input)
        rel_plus_btn = QtWidgets.QPushButton(tr("Relative +"))
        rel_plus_btn.setStyleSheet(_COMPACT_BTN_STYLE)
        rel_plus_btn.clicked.connect(lambda: self.move_relative(self.rel_step_input.value()))
        rel_layout.addWidget(rel_plus_btn)

        layout.addLayout(rel_layout)


        speed_layout = QtWidgets.QHBoxLayout()
        speed_label = QtWidgets.QLabel(tr("Speed:"))
        speed_layout.addWidget(speed_label)

        self.speed_group = QtWidgets.QButtonGroup(self)
        self.radio_high = QtWidgets.QRadioButton(tr("High"))
        self.radio_med = QtWidgets.QRadioButton(tr("Medium"))
        self.radio_low = QtWidgets.QRadioButton(tr("Low"))
        
        self.speed_group.addButton(self.radio_high)
        self.speed_group.addButton(self.radio_med)
        self.speed_group.addButton(self.radio_low)
        
        speed_layout.addWidget(self.radio_high)
        speed_layout.addWidget(self.radio_med)
        speed_layout.addWidget(self.radio_low)
        layout.addLayout(speed_layout)
        
        # Initialize speed setting from the controller
        ch_str = self.controller.stringify_ch_numbers(self.ch_number)
        if ch_str:
            try:
                # Query current speed
                speed_resp = self.controller.get_ch_speed(self.ch_number)
                if speed_resp:
                    if "HSPD" in speed_resp:
                        self.radio_high.setChecked(True)
                        self.current_speed = "H"
                    elif "MSPD" in speed_resp:
                        self.radio_med.setChecked(True)
                        self.current_speed = "M"
                    elif "LSPD" in speed_resp:
                        self.radio_low.setChecked(True)
                        self.current_speed = "L"

                
                curret_pos = self.controller.get_ch_pos(self.ch_number)
                if curret_pos:
                    self.pos_label.setText(f"{int(curret_pos):+}")
                    self.target_input.setValue(int(curret_pos))
            except Exception as e:
                print(f"Error reading speed for Ch{self.ch_number}: {e}")
                
        # Connect speed signals to update the controller
        self.radio_high.clicked.connect(lambda: self.set_speed("H"))
        self.radio_med.clicked.connect(lambda: self.set_speed("M"))
        self.radio_low.clicked.connect(lambda: self.set_speed("L"))

        layout.addStretch()
        
    def move_absolute(self):
        """Move to absolute position"""
        try:
            self.controller.move_ch_absolute(self.ch_number, self.target_input.value())
            MovementWarningDialog(self.controller, self).exec()
            self.controller.wait_until_stop()
            self.update_position(update_input=True)
            self.controller.switch_to_loc()
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, tr("Software Limit"), str(e))
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, tr("Error"), tr("Failed to move: {error}", error=e))

    def move_relative(self, steps):
        """Move relative to current position"""
        try:
            self.set_speed(self.current_speed)
            self.controller.move_ch_relative(self.ch_number, steps)
            print("Relative move command sent",
                  f"\nCh: {self.controller.stringify_ch_numbers(self.ch_number)}, relative_move: {steps}")
            MovementWarningDialog(self.controller, self).exec()
            self.controller.wait_until_stop()
            self.update_position(update_input=True)
            self.controller.switch_to_loc()
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, tr("Software Limit"), str(e))
        except Exception as e:
            print(f"{e.__class__.__name__}: {e}")
            QtWidgets.QMessageBox.critical(self, tr("Error"), tr("Failed to move: {error}", error=e))

    def update_position(self, update_input=False):
        """Update the displayed position"""
        try:
            pos = self.controller.get_ch_pos(self.ch_number)
            print(f"Ch{self.ch_number} position: {pos}")
            if pos:
                self.pos_label.setText(f"{int(pos):+}")
                if update_input:
                    print("Updating input value without triggering signals...")
                    self.target_input.blockSignals(True)
                    self.target_input.setValue(int(pos))
                    self.target_input.blockSignals(False)
            else:
                self.pos_label.setText(tr("ERROR"))
        except Exception as e:
            print(f"{e.__class__.__name__}: {e}")
            self.pos_label.setText("ERROR")
    
    def set_speed(self, level):
        """level is 'L', 'M', or 'H'."""
        try:
            self.controller.set_ch_speed(self.ch_number, level)
        except Exception as e:
            print(f"{e.__class__.__name__}: {e}")
            QtWidgets.QMessageBox.critical(self, tr("Error"), tr("Failed to set speed: {error}", error=e))


class MovementWarningDialog(QtWidgets.QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle(tr("CAUTION: Stage in Motion"))

        # Make it a modal window so the user can't click the main app while moving
        self.setModal(True)
        self.setFixedSize(700, 200)

        layout = QtWidgets.QVBoxLayout(self)

        # 1. Caution Message
        self.warning_label = QtWidgets.QLabel(tr("CAUTION: STAGE IS IN MOTION"))
        self.warning_label.setStyleSheet("color: red; font-size: 24px;")
        self.warning_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.warning_label)

        # 2. Video Feeds
        # video_layout = QtWidgetsself.controller.stringify_ch_numbers(self.ch_number).QHBoxLayout()
        # self.cam1_label = QtWidgets.QLabel("Loading Cam 71...")
        # self.cam2_label = QtWidgets.QLabel("Loading Cam 70...")
        # self.cam1_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        # self.cam2_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        # self.cam1_label.setFixedSize(320, 240)
        # self.cam2_label.setFixedSize(320, 240)
        # self.cam1_label.setStyleSheet("background-color: black; color: white;")
        # self.cam2_label.setStyleSheet("background-color: black; color: white;")
        # video_layout.addWidget(self.cam1_label)
        # video_layout.addWidget(self.cam2_label)
        # layout.addLayout(video_layout)

        # 3. Stop Buttons
        stop_btn_layout = QtWidgets.QHBoxLayout()

        self.btn_estop = QtWidgets.QPushButton(tr("EMERGENCY STOP"))
        self.btn_estop.setStyleSheet("background-color: red; color: white; font-size: 20px; font-weight: bold; padding: 15px;")
        self.btn_estop.clicked.connect(self.emergency_stop)
        stop_btn_layout.addWidget(self.btn_estop)

        self.btn_nstop = QtWidgets.QPushButton(tr("Normal Stop"))
        self.btn_nstop.setStyleSheet("background-color: orange; color: white; font-size: 20px; font-weight: bold; padding: 15px;")
        self.btn_nstop.clicked.connect(self.normal_stop)
        stop_btn_layout.addWidget(self.btn_nstop)

        layout.addLayout(stop_btn_layout)

        # 4. Open Network Streams
        # NOTE: If your cameras serve an HTML dashboard at the root URL, OpenCV will fail to read it.
        # You may need to append the direct video path (e.g., "http://130.87.177.71/video.mjpg")
        # self.cap1 = cv2.VideoCapture("http://130.87.177.71/ViewerFrame?Resolution=640x480&Quality=Standard&Size=STD&Language=1&Sound=Enable&Mode=JPEG&RPeriod=65535&SendMethod=1&View=Full")
        # self.cap2 = cv2.VideoCapture("http://130.87.177.70/ViewerFrame?Resolution=640x480&Quality=Standard&Size=STD&Language=1&Sound=Enable&Mode=JPEG&RPeriod=65535&SendMethod=1&View=Full")

        # 5. Polling Timer (Replaces the time.sleep() blocking loop)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_dialog)
        self.timer.start(200)  # Update videos & check status every 200ms

    def update_dialog(self):
        # Check if stage has stopped — works for both real controller and simulator
        if not self.controller.get_is_moving():
            self.finish_and_close()

    def _update_label_with_frame(self, cap, label):
        if cap.isOpened():
            ret, frame = cap.read()
            if ret:
                frame = cv2.resize(frame, (320, 240))
                rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb_image.shape
                qimg = QtGui.QImage(rgb_image.data, w, h, ch * w, QtGui.QImage.Format.Format_RGB888)
                label.setPixmap(QtGui.QPixmap.fromImage(qimg))

    def emergency_stop(self):
        self.controller.emergency_stop()
        self.warning_label.setText(tr("🛑 EMERGENCY STOP ACTIVATED 🛑"))
        self.finish_and_close()

    def normal_stop(self):
        self.controller.normal_stop()
        self.warning_label.setText(tr("Normal Stop sent — decelerating..."))

    def finish_and_close(self):
        self.timer.stop()
        # if self.cap1.isOpened(): self.cap1.release()
        # if self.cap2.isOpened(): self.cap2.release()
        self.controller.switch_to_loc()
        self.accept()

    def closeEvent(self, event):
        self.finish_and_close()
        super().closeEvent(event)


class StageControllerApp(QtWidgets.QMainWindow):
    """Main application window for stage control"""

    def __init__(self, controller=None):
        super().__init__()
        self.setWindowTitle(tr("Stage Motor Controller"))
        self.resize(1400, 700)

        if controller is not None:
            self.controller = controller
            self._owns_controller = False
        else:
            self.controller = PM16CController(ip='192.168.1.55', port=7777, debug=True)
            try:
                self.controller.connect()
            except Exception as e:
                print(f"{e.__class__.__name__}: {e}")
                QtWidgets.QMessageBox.critical(self, tr("Connection Error"), tr("Could not connect: {error}", error=e))
                raise
            self._owns_controller = True

        # Create main layout
        main_layout = QtWidgets.QVBoxLayout()

        # Title
        title_label = QtWidgets.QLabel(tr("BL-18C PM16C Motor Controller - Manual Control"))
        title_font = title_label.font()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title_label.setFont(title_font)
        main_layout.addWidget(title_label)
        
        # Motor control widgets
        motors_layout = QtWidgets.QVBoxLayout()
        motors_layout.setSpacing(1)
        self.motor_widgets = []
        
        for ch in range(1, 12):  # Channels 1-11
            widget = MotorControlWidget(ch, self.controller)
            motors_layout.addWidget(widget)
            self.motor_widgets.append(widget)
            
        
        motors_group = QtWidgets.QGroupBox(tr("Motor Controls"))
        motors_group.setLayout(motors_layout)

        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidget(motors_group)
        scroll_area.setWidgetResizable(True)
        main_layout.addWidget(scroll_area, stretch=1)

        # Control buttons
        button_layout = QtWidgets.QHBoxLayout()

        refresh_btn = QtWidgets.QPushButton(tr("Refresh All Positions"))
        refresh_btn.clicked.connect(lambda: self.refresh_all_positions(update_input=True))
        button_layout.addWidget(refresh_btn)

        normal_stop_btn = QtWidgets.QPushButton(tr("Normal Stop All Motors"))
        normal_stop_btn.setStyleSheet("background-color: orange; color: white; font-weight: bold;")
        normal_stop_btn.clicked.connect(self.normal_stop_all_motors)
        button_layout.addWidget(normal_stop_btn)

        estop_btn = QtWidgets.QPushButton(tr("Emergency Stop All Motors"))
        estop_btn.setStyleSheet("background-color: #ff6b6b; color: white; font-weight: bold;")
        estop_btn.clicked.connect(self.stop_all_motors)
        button_layout.addWidget(estop_btn)

        status_label = QtWidgets.QLabel(tr("Ready"))
        button_layout.addWidget(status_label)
        self.status_label = status_label
        
        button_layout.addStretch()
        main_layout.addLayout(button_layout)
        
        # Central widget
        central_widget = QtWidgets.QWidget(self)
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)
        
        # Timer to periodically update positions
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.timeout.connect(self.refresh_all_positions)
        # self.update_timer.start(5000)  # Update every 5 seconds
        
        # Initial position update
        self.refresh_all_positions(update_input=True)
        

    def get_all_limits(self):
        """Get forward and backward limits for all channels"""
        limits_info = []
        for ch in range(1, 12):
            try:
                bl = self.controller.read_backward_limit(ch)
                fl = self.controller.read_forward_limit(ch)
                limits_info.append(f"Ch{ch}: BL={bl}, FL={fl}")
            except Exception as e:
                limits_info.append(f"Ch{ch}: Error reading limits ({e})")
        print("\n".join(limits_info))
        return "\n".join(limits_info)
    
    def refresh_all_positions(self, update_input=False):
        """Refresh position displays for all motors"""
        if not self.isActiveWindow():
            self.status_label.setText(tr("Paused (window inactive)"))
            return
        try:
            for widget in self.motor_widgets:
                widget.update_position(update_input=update_input)
            self.status_label.setText(tr("Ready"))
        except Exception as e:
            print(f"{e.__class__.__name__}: {e}")
            self.status_label.setText(tr("Error: {msg}", msg=e))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                # self.update_timer.start(5000)
                QtCore.QTimer.singleShot(0, lambda: self.refresh_all_positions(update_input=True))
            else:
                self.update_timer.stop()
                try:
                    self.controller.switch_to_loc()
                except Exception:
                    pass
                self.status_label.setText(tr("Paused (window inactive)"))

    def normal_stop_all_motors(self):
        try:
            self.controller.normal_stop()
            self.status_label.setText(tr("Normal stop sent"))
        except Exception as e:
            print(f"{e.__class__.__name__}: {e}")
            self.status_label.setText(tr("Error stopping motors: {error}", error=e))

    def stop_all_motors(self):
        """Emergency stop all motors"""
        try:
            self.controller.emergency_stop()
            self.status_label.setText(tr("Emergency stop activated"))
        except Exception as e:
            print(f"{e.__class__.__name__}: {e}")
            self.status_label.setText(tr("Error stopping motors: {error}", error=e))
    
    def closeEvent(self, event):
        """Clean up when closing application"""
        self.update_timer.stop()
        if self._owns_controller:
            try:
                self.controller.switch_to_loc()
                self.controller.disconnect()
            except Exception as e:
                print(f"{e.__class__.__name__}: {e}")
        event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    try:
        window = StageControllerApp()
    except Exception:
        sys.exit(1)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

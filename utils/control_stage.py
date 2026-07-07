import socket
import threading
import time
import sys
import numpy as np
import math
from operator import ge, le, gt, lt, eq

# ---------------------------------------------------------------------------
# Move constraints (inter-channel software limits)
#
# Each rule is evaluated before every absolute or relative move.
# If the intended target position of `target_ch` satisfies (`target_op`,
# `target_val`), then the *current* position of `required_ch` must satisfy
# (`required_op`, `required_val`) — otherwise the move is rejected.
#
# To add a new constraint, append a dict with the five keys shown below.
# ---------------------------------------------------------------------------
# Collision boundary between the Detector (Ch9) and Microscope arm (Ch8).
# Ch9 must be at or beyond this pulse position (i.e. ≤ value) before Ch8 can
# move into the beam path (positive direction), and vice versa.
# This constant is the single source of truth: MOVE_CONSTRAINTS below and all
# UI-level validation code import or reference it.
CH9_CH8_SAFE_BOUNDARY = -30000

MOVE_CONSTRAINTS = [
    # Ch9 > CH9_CH8_SAFE_BOUNDARY requires Ch8 <= 0
    # Moving Ch9 TO the boundary or more negative (OUT direction) is always safe.
    # Only moving Ch9 INTO the beam path is restricted.
    {
        'target_ch': 9, 'target_op': '>', 'target_val': CH9_CH8_SAFE_BOUNDARY,
        'required': [
            {'ch': 8, 'op': '<=', 'val': 0},
        ],
    },
    # Ch8 > 0 requires Ch9 <= CH9_CH8_SAFE_BOUNDARY
    # Moving Ch8 TO 0 or more negative (OUT direction) is always safe.
    # Only moving Ch8 INTO the beam path is restricted.
    {
        'target_ch': 8, 'target_op': '>', 'target_val': 0,
        'required': [
            {'ch': 9, 'op': '<=', 'val': CH9_CH8_SAFE_BOUNDARY},
        ],
    }
]

_OPS = {'>=': ge, '<=': le, '>': gt, '<': lt, '==': eq}

# ---------------------------------------------------------------------------
# Pulse-to-physical-unit conversion for each channel
# Translation stages (Ch1–10): µm/pulse
# Rotation stage (Ch11): degrees/pulse
# ---------------------------------------------------------------------------
PULSE_SCALE: dict[int, float] = {
    1:  1.0,    # µm/pulse
    2:  2.0,    # µm/pulse
    3:  2.0,    # µm/pulse  Focus X
    4:  2.0,    # µm/pulse  Sample Y
    5:  0.11,   # µm/pulse  Sample Z
    6:  1.0,    # µm/pulse Microscope Z
    7:  0.2,    # µm/pulse Microscope X
    8:  1.0,    # µm/pulse  Microscope Y
    9:  10.0,   # µm/pulse  Detector (IN/OUT, X) 
    10: 2.0,    # µm/pulse
    11: 0.004,  # deg/pulse
}

class PM16CController:
    def __init__(self, ip, port, debug=False):
        self.ip = ip
        self.port = port
        self.debug = debug
        self.terminator = '\r\n'
        self.client = None
        self._lock = threading.Lock()

    def connect(self):
        """ Connect the controller and delete remaining buffers if exist """
        print(f"Attempting to connect, {self.ip}:{self.port}...")
        self.client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.client.settimeout(2.0)
        self.client.connect((self.ip, self.port))
        
        # Delete rec. buffer
        self.client.settimeout(0.1)
        try:
            while True:
                self.client.recv(1024)
        except socket.timeout:
            pass
        self.client.settimeout(2.0)
        print(f"Connected to the stepping motor controller at {self.ip} (PORT: {self.port})")

    def disconnect(self):
        """ Disconnect from the contrlller """
        if self.client:
            self.client.close()
            print("Disconnected.")

    def send_cmd(self, cmd, has_response=True):
        """
        Send a command to the controller.
        Acquires a lock so concurrent threads don't interleave commands/responses.
        """
        with self._lock:
            full_cmd = f"{cmd}{self.terminator}"
            self.client.sendall(full_cmd.encode('ascii'))

            if not has_response:
                if self.debug: print(f"Sending: {cmd:<10} without waiting for the response")
                return None

            # 応答を待つ処理
            response = b""
            try:
                while True:
                    chunk = self.client.recv(1024)
                    if not chunk:
                        break
                    response += chunk
                    if self.terminator.encode('ascii') in response:
                        break
                # Take only the first complete line in case the buffer held
                # multiple responses from a previous concurrent call.
                res_str = response.decode('ascii').split('\r\n')[0].strip()
                if self.debug: print(f"Command: {cmd:<10} -> Response: {res_str}")
                return res_str

            except socket.timeout:
                print(f"Error: '{cmd}' timed out")
                return None

    def switch_to_rem(self):
        self.send_cmd("REM", has_response=False)

    def switch_to_loc(self):
        self.send_cmd("LOC", has_response=False)

    def is_all_motors_stopped(self, status_string):
        # STS? response: R(L)abcd/PNNS/VVVV/HHJJKKLL/±pos...
        # PNNS: 'P'=cw moving, 'N'=ccw moving, 'S'=stopped
        if status_string is None:
            return False
        parts = status_string.strip().split("/")
        if len(parts) < 2:
            return False
        return all(c == 'S' for c in parts[1])

    
    def wait_until_stop(self, confirm_count=4, stay_in_rem=False):
        """ check the current status and wait until the motors are stopped """
        if self.debug: print("--- Waiting until the operation is completed ---")
        consecutive = 0
        while True:
            if self.is_all_motors_stopped(self.send_cmd("STS?", has_response=True)):
                consecutive += 1
                if consecutive >= confirm_count:
                    break
            else:
                consecutive = 0
            time.sleep(0.1)

        if stay_in_rem:
            if self.debug: print("--- Operation completed --- (staying in REM)")
            return
        if self.debug: print("--- Operation completed ---\n--- Switch to LOC ---")
        self.switch_to_loc()

    def print_invalid_ch(self):
        print("Invalid ch input.")

    def stringify_ch_numbers(self, ch):
        if ch <= 0 or ch >= 12:
            self.print_invalid_ch()
            return None # error
        elif 1 <= ch <= 9:
            return f"{ch}"
        elif ch == 10:
            return "A"
        elif ch == 11:
            return "B"
        else: 
            self.print_invalid_ch()
            return None
            
    def check_move_constraints(self, ch, target_pos):
        """Check MOVE_CONSTRAINTS before a move.

        Returns (True, "") when safe.
        Returns (False, reason) when a constraint would be violated.
        Each rule's 'required' list is checked in order; all conditions must hold.
        """
        for rule in MOVE_CONSTRAINTS:
            if rule['target_ch'] != ch:
                continue
            if not _OPS[rule['target_op']](target_pos, rule['target_val']):
                continue
            for req in rule['required']:
                req_str = self.get_ch_pos(req['ch'])
                if req_str is None:
                    return False, (
                        f"Cannot read Ch{req['ch']} position "
                        f"(required for limit check on Ch{ch})"
                    )
                if not _OPS[req['op']](int(req_str), req['val']):
                    return False, (
                        f"Move blocked: Ch{ch} → {target_pos:+} requires "
                        f"Ch{req['ch']} {req['op']} {req['val']:+}, "
                        f"but current position is {int(req_str):+}"
                    )
        return True, ""

    def move_ch_relative(self, ch, diff):
        current_str = self.get_ch_pos(ch)
        if current_str is None:
            raise ValueError(
                f"Ch{ch} の現在位置を取得できませんでした。\n"
                "通信エラーの可能性があるため、衝突防止のため相対値移動をブロックしました。"
            )
        ok, msg = self.check_move_constraints(ch, int(current_str) + diff)
        if not ok:
            raise ValueError(msg)
        self.switch_to_rem()
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return None
        self.send_cmd(f"REL{ch_str}{diff:+}", has_response=False)

    def move_ch_absolute(self, ch, target):
        ok, msg = self.check_move_constraints(ch, target)
        if not ok:
            raise ValueError(msg)
        self.switch_to_rem()
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return None
        self.send_cmd(f"ABS{ch_str}{target:+}", has_response=False)

    def get_ch_pos(self, ch):
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is not None:
            response = self.send_cmd(f"STS{ch_str}?")
            if response:
                return response[6:]
            return None
        
    def get_ch_status(self, ch):
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is not None:
            return self.send_cmd(f"STS{ch_str}?")

    def get_status(self):
        return self.send_cmd("STS?")
    
    def get_is_moving(self):
        response = self.send_cmd("STS?")
        if response is None:
            return False
        return not self.is_all_motors_stopped(response)
    
    def get_ch_backlash(self, ch):
        return self.send_cmd(f"B{ch}?")
    
    def set_ch_backlash(self, ch, target):
        self.switch_to_rem()
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is not None:
            self.send_cmd(f"B{ch_str}{target:+04}", has_response=False)
        self.switch_to_loc()
    
    def get_ch_spped(self, ch):
        """ return HSPD, MSPD, LSPD """
        ch_str = self.stringify_ch_numbers(ch)
        return self.send_cmd(f"SPD?{ch_str}")

    def get_ch_speed_value(self, ch, level: str) -> "int | None":
        """Read the actual pps register value for channel ch's L/M/H speed setting.

        *level* is one of 'L', 'M', 'H'. Returns pps as int, or None on error.
        """
        if level not in ("L", "M", "H"):
            return None
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return None
        response = self.send_cmd(f"SPD{level}?{ch_str}")
        if response is None:
            return None
        try:
            return int(response.strip())
        except ValueError:
            return None

    def set_ch_speed_value(self, ch, level: str, pps: int) -> None:
        """Set the actual pps register value for channel ch's L/M/H speed setting."""
        if level not in ("L", "M", "H"):
            return
        ch_str = self.stringify_ch_numbers(ch)
        if ch_str is None:
            return
        self.switch_to_rem()
        self.send_cmd(f"SPD{level}{ch_str}{pps}", has_response=False)

    def get_ch_lspd(self, ch) -> "int | None":
        """Read the LSPD register value for channel ch.  Returns pps as int, or None on error."""
        return self.get_ch_speed_value(ch, "L")

    def set_ch_lspd(self, ch, pps: int) -> None:
        """Set the LSPD register for channel ch to pps [pulses per second]."""
        self.set_ch_speed_value(ch, "L", pps)

    def set_ch_speed(self, ch, speed="M"):
        self.switch_to_rem()
        ch_str = self.stringify_ch_numbers(ch)
        if speed in ["L", "M", "H"]:
            self.send_cmd(f"SPD{speed}{ch_str}", has_response=False)

    def read_backward_limit(self, ch):
        ch_str = self.stringify_ch_numbers(ch)
        return self.send_cmd(f"BL?{ch_str}")
    
    def read_forward_limit(self, ch):
        ch_str = self.stringify_ch_numbers(ch)
        return self.send_cmd(f"FL?{ch_str}")

    def normal_stop(self):
        self.send_cmd("ASSTP")
        self.switch_to_loc()

    def emergency_stop(self):
        self.send_cmd("AESTP")
        self.switch_to_loc()




        



# def confirm_next_step(message=""):
#     """ Asks the user whether they wish to proceed to the next step """
#     print("Go to the next step? (Y/N)")
#     while True:
#         # Delete the space(s) and capitalise the letters
#         user_input = input(message).strip().upper()
        
#         if user_input == 'Y':
#             return True
#         elif user_input == 'N':
#             print("Terminate the loop")
#             sys.exit() 
#         else:
#             print("Error: Input either Y or N.")




# controller = PM16CController(ip='192.168.1.55', port=7777, debug=True)

# try:
#     controller.connect()

#     # Swich to the remote control mode
#     # controller.send_cmd("REM", has_response=False)

#     controller.send_cmd("STS?", has_response=True)

    
    
# finally:
#     controller.send_cmd("LOC", has_response=False)
#     controller.disconnect()
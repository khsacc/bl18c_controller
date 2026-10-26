"""Lifecycle for the Windows PF ring STARS bridge supplied on the control PC."""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path


class RingBridgeError(RuntimeError):
    pass


def _ring_process_running() -> bool:
    """Find a manually started bridge without depending on tasklist or psutil."""
    if sys.platform != "win32":
        return False

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        raise RingBridgeError(f"Could not inspect ring bridge processes: {ctypes.WinError(ctypes.get_last_error())}")
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            error = ctypes.get_last_error()
            if error == 18:  # ERROR_NO_MORE_FILES
                return False
            raise RingBridgeError(f"Could not inspect ring bridge processes: {ctypes.WinError(error)}")
        while True:
            if entry.szExeFile.casefold() == "ringepf.exe":
                return True
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error == 18:
                    return False
                raise RingBridgeError(f"Could not inspect ring bridge processes: {ctypes.WinError(error)}")
    finally:
        kernel32.CloseHandle(snapshot)


class RingBridge:
    """Start one local bridge if needed and stop only a process we started."""

    def __init__(self, directory: Path, server_ip: str):
        self.directory = directory
        self.server_ip = server_ip
        self._process: subprocess.Popen | None = None

    def ensure_running(self) -> bool:
        """Return True only when this call starts a new bridge process."""
        if sys.platform != "win32":
            return False
        if self._process is not None:
            exit_code = self._process.poll()
            if exit_code is None:
                return False
            logging.warning("PF ring bridge exited with code %s; restarting", exit_code)
            self._process = None
        if _ring_process_running():
            return False

        executable = self.directory / "ringEPF.exe"
        database = self.directory / "cshosts.csv"
        if not executable.is_file() or not database.is_file():
            raise RingBridgeError(
                f"PF ring client is incomplete in {self.directory}; "
                "ringEPF.exe and cshosts.csv are required"
            )
        env = os.environ.copy()
        env["TINE_HOME"] = "."
        try:
            self._process = subprocess.Popen(
                [str(executable), "Ring", self.server_ip],
                cwd=str(self.directory),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise RingBridgeError(f"Could not start PF ring client: {exc}") from exc
        return True

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            pass

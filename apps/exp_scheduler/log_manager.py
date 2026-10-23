"""
RunLogger — file-based logging for SequenceRunner.

Each run gets one directory, <data_dir>/<run_id>/ (default data_dir:
__localdata/runs/), holding every file the run produces in category
subdirectories (see RUN_SUBDIRS): XRD/, spectrum/, photo/ and log/.
The logger writes four files under <run_id>/log/:
  sequence.json  — loadable sequence and current global settings
  metadata.json   — sequence JSON + config snapshot (written once at start)
  conditions.csv  — time-series T/P/stage/XRD data for scientific reference
  ops.log         — all device commands and events for debugging

Usage::
    logger = RunLogger(ctx)
    logger.start(path="run_001", devices=["pace5000", "lakeshore"], ...)
    logger.log_ops("[CMD LAKESHORE] set_setpoint(300.0 K)")
    logger.log_science("xrd_taken", xrd_file="scan_001.npy")
    logger.stop()
"""
from __future__ import annotations

import csv
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .device_context import DeviceContext

_CONDITIONS_FIELDS = [
    "timestamp", "elapsed_s", "event_type", "step_index",
    "T_K", "P_MPa", "Ch3_pulse", "Ch4_pulse", "Ch5_pulse",
    "xrd_file", "spectrum_file", "note",
]

DEFAULT_POLL_INTERVAL_S = 30.0
_DEFAULT_DATA_SUBDIR = "__localdata/runs"

# Category subdirectories created on demand under each run directory.
XRD_SUBDIR = "XRD"
SPECTRUM_SUBDIR = "spectrum"
PHOTO_SUBDIR = "photo"
LOG_SUBDIR = "log"
RUN_SUBDIRS = (XRD_SUBDIR, SPECTRUM_SUBDIR, PHOTO_SUBDIR, LOG_SUBDIR)


class RunLogger:
    """
    Thread-safe logger for a single sequence run.

    All public methods are no-ops when the logger has not been started (or has
    already been stopped), so callers never need to guard with ``if self._logger``.
    """

    def __init__(self, ctx: "DeviceContext",
                 on_ops_line: Callable[[str], None] | None = None) -> None:
        self._ctx = ctx
        # Called (from the writing thread) with each line written to ops.log,
        # without the trailing newline — drives the scheduler's live log view.
        self._on_ops_line = on_ops_line
        self._devices: list[str] = []
        self._run_dir: Path | None = None
        self._log_dir: Path | None = None
        self._science_file = None
        self._science_writer: csv.DictWriter | None = None
        self._ops_file = None
        self._lock = threading.Lock()
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
        self._t0: float = 0.0
        self._active = False

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(
        self,
        path: str,
        devices: list[str],
        sequence_dict: dict,
        global_limits_dict: dict,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        data_dir: Path | str | None = None,
    ) -> None:
        """Open log files and start the background polling thread.

        path         — base name used for the run directory (e.g. "run_001")
        devices      — list of device keys to poll: "pace5000", "lakeshore"
        poll_interval_s — how often to write a "periodic" row to conditions.csv
        data_dir     — directory under which the <run_id> folder is created.
                       Defaults to __localdata/runs/ next to this module.
        """
        if self._active:
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = Path(path).stem
        run_id = f"{stem}_{ts}"
        base = Path(data_dir) if data_dir else (Path(__file__).parent / _DEFAULT_DATA_SUBDIR)
        base.mkdir(parents=True, exist_ok=True)
        suffix = 1
        while True:
            candidate = base / (run_id if suffix == 1 else f"{run_id}_{suffix}")
            try:
                candidate.mkdir()
                break
            except FileExistsError:
                suffix += 1
        self._run_dir = candidate
        self._log_dir = candidate / LOG_SUBDIR
        self._log_dir.mkdir()
        run_id = candidate.name

        self._devices = list(devices)
        self._poll_interval_s = max(1.0, poll_interval_s)
        self._t0 = time.monotonic()

        # This is the same top-level format as Save Sequence / Load Sequence.
        (self._log_dir / "sequence.json").write_text(
            json.dumps(sequence_dict, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # metadata.json — written once before opening CSV/log so that even a
        # crashed run leaves a readable record of what was attempted.
        meta = {
            "run_id": run_id,
            "started_at": datetime.now().isoformat(timespec="milliseconds"),
            "devices_logged": self._devices,
            "science_poll_interval_s": self._poll_interval_s,
            "global_limits": global_limits_dict,
            "sequence": sequence_dict,
        }
        (self._log_dir / "metadata.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Open output files, then set _active so log_* methods become live.
        # Close a partially opened CSV if opening the ops log fails.
        try:
            self._science_file = open(
                self._log_dir / "conditions.csv", "w", newline="", encoding="utf-8"
            )
            self._science_writer = csv.DictWriter(
                self._science_file, fieldnames=_CONDITIONS_FIELDS
            )
            self._science_writer.writeheader()
            self._science_file.flush()
            self._ops_file = open(self._log_dir / "ops.log", "w", encoding="utf-8")
        except Exception:
            if self._science_file is not None:
                self._science_file.close()
                self._science_file = None
                self._science_writer = None
            raise

        self._active = True

        self.log_ops(f"[SEQ:START] {run_id}")
        self.log_science("start", note="Sequence started")

        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="RunLogger-poll"
        )
        self._poll_thread.start()

    def stop(self) -> None:
        """Stop polling thread and close log files.

        Callers should write any final log_ops / log_science entries BEFORE
        calling stop(), because this method sets _active=False and flushes.
        """
        if not self._active:
            return

        self._poll_stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None

        self._active = False

        with self._lock:
            if self._science_file is not None:
                self._science_file.flush()
                self._science_file.close()
                self._science_file = None
                self._science_writer = None
            if self._ops_file is not None:
                self._ops_file.flush()
                self._ops_file.close()
                self._ops_file = None

    @property
    def run_dir(self) -> Path | None:
        """<data_dir>/<run_id>/ — parent of every category subdirectory."""
        return self._run_dir

    @property
    def log_dir(self) -> Path | None:
        """<run_dir>/log/ — where the logger's own four files are written."""
        return self._log_dir

    # ── conditions.csv ───────────────────────────────────────────────────

    def log_science(
        self,
        event_type: str,
        step_index: int | None = None,
        note: str = "",
        xrd_file: str = "",
        spectrum_file: str = "",
    ) -> None:
        """Append one row to conditions.csv.

        Reads T, P, and stage positions at the moment of the call.
        Columns that cannot be read (device absent or not in *devices*) are left empty.

        event_type examples:
            start, stop, periodic, xrd_taken, pressure_reached,
            temperature_reached, user_log, error, logging_stopped
        """
        if not self._active:
            return

        now = datetime.now()
        elapsed = time.monotonic() - self._t0
        t_k = self._read_temperature()
        p_mpa = self._read_pressure()
        ch3, ch4, ch5 = self._read_stage_positions()

        row = {
            "timestamp": now.isoformat(timespec="milliseconds"),
            "elapsed_s": f"{elapsed:.1f}",
            "event_type": event_type,
            "step_index": "" if step_index is None else step_index,
            "T_K":        "" if t_k is None else f"{t_k:.3f}",
            "P_MPa":      "" if p_mpa is None else f"{p_mpa:.4f}",
            "Ch3_pulse":  "" if ch3 is None else ch3,
            "Ch4_pulse":  "" if ch4 is None else ch4,
            "Ch5_pulse":  "" if ch5 is None else ch5,
            "xrd_file":   xrd_file,
            "spectrum_file": spectrum_file,
            "note":       note,
        }

        with self._lock:
            if self._science_writer is not None and self._science_file is not None:
                self._science_writer.writerow(row)
                self._science_file.flush()

    # ── ops.log ──────────────────────────────────────────────────────────

    def log_ops(self, message: str) -> None:
        """Append a timestamped line to ops.log.

        Safe to call from any thread (protected by self._lock).
        Silently no-ops if the logger is not started.
        """
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        line = f"{ts} {message}"
        with self._lock:
            if self._ops_file is None:
                return
            self._ops_file.write(line + "\n")
            self._ops_file.flush()
        if self._on_ops_line is not None:
            try:
                self._on_ops_line(line)
            except Exception:
                pass  # a display failure must never break the run

    # ── internal ─────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Background thread: write a "periodic" row to conditions.csv every N seconds."""
        while not self._poll_stop.wait(timeout=self._poll_interval_s):
            try:
                self.log_science("periodic")
            except Exception:
                pass

    def _read_temperature(self) -> float | None:
        if "lakeshore" not in self._devices:
            return None
        try:
            backend = self._ctx.lakeshore
            if backend is None:
                return None
            data = backend.get_data()
            return data[-1].temp_a_k if data else None
        except Exception:
            return None

    def _read_pressure(self) -> float | None:
        if "pace5000" not in self._devices:
            return None
        try:
            backend = self._ctx.pace5000
            if backend is None:
                return None
            val = backend.get_pressure()
            return float(val) if val is not None else None
        except Exception:
            return None

    def _read_stage_positions(self) -> tuple[int | None, int | None, int | None]:
        try:
            ctrl = self._ctx.controller
            if ctrl is None:
                return None, None, None
            return (
                _safe_get_pos(ctrl, 3),
                _safe_get_pos(ctrl, 4),
                _safe_get_pos(ctrl, 5),
            )
        except Exception:
            return None, None, None


def _safe_get_pos(ctrl, ch: int) -> int | None:
    try:
        return ctrl.get_ch_pos(ch)
    except Exception:
        return None

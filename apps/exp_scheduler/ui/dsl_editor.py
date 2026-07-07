"""
DslEditor — DSL script editor widget for the Experimental Scheduler.

Provides:
  - QPlainTextEdit for DSL text input
  - [Validate] button: runs ASTValidator, shows line-numbered errors below
  - [Convert to Visual →] button: validates + parses → emits sequence_changed(Sequence)
  - set_sequence(): converts a Sequence to DSL text (for Visual → Script sync)
"""
from __future__ import annotations

import ast

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..dsl.parser import SequenceBuilder
from ..dsl.validator import ASTValidator
from ..sequence import Sequence


class DslEditor(QWidget):
    """
    DSL script editor widget.

    Signals:
        sequence_changed(object) — emitted when the user clicks
            "Convert to Visual" and parsing succeeds.  Carries a Sequence.
    """

    sequence_changed = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_validator = None  # callable(Sequence) -> PreCheckResult | None
        self._build_ui()

    def set_full_validator(self, fn) -> None:
        """Set a callback for full (hardware-aware) validation.

        fn(seq: Sequence) -> PreCheckResult
        Called after structural checks pass.  If the result has no errors, the
        caller is expected to enable the Run button.
        """
        self._full_validator = fn

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        # Toolbar
        bar = QHBoxLayout()

        self._btn_validate = QPushButton("Validate")
        self._btn_validate.setToolTip("Check DSL for syntax and semantic errors")
        self._btn_validate.clicked.connect(self._on_validate)
        bar.addWidget(self._btn_validate)

        self._btn_convert = QPushButton("Convert to Visual →")
        self._btn_convert.setToolTip(
            "Parse the DSL script and update the Visual timeline"
        )
        self._btn_convert.clicked.connect(self._on_convert)
        bar.addWidget(self._btn_convert)

        bar.addStretch()
        root.addLayout(bar)

        # Text editor
        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText(
            "# Write your DSL experiment script here.\n"
            "#\n"
            "# Example:\n"
            '# wait(duration=5, unit="min")\n'
            '# set_pressure(pressure=1.0, unit="MPa", rate=0.05, rate_unit="MPa/min")\n'
            '# wait_pressure(tol=0.001, unit="MPa")\n'
            '# take_xrd(exposure_ms=1000, save=True, prefix="scan")'
        )
        font = self._editor.font()
        font.setFamily("Courier New")
        font.setPointSize(10)
        self._editor.setFont(font)
        root.addWidget(self._editor, stretch=1)

        # Status area
        root.addWidget(QLabel("Status:"))
        self._status = QTextEdit()
        self._status.setReadOnly(True)
        self._status.setMaximumHeight(110)
        self._status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        font2 = self._status.font()
        font2.setFamily("Courier New")
        font2.setPointSize(9)
        self._status.setFont(font2)
        root.addWidget(self._status)

    # ── Public API ─────────────────────────────────────────────────────────

    def get_text(self) -> str:
        """Return the current DSL text."""
        return self._editor.toPlainText()

    def set_text(self, text: str) -> None:
        """Replace the editor content.  Clears the status area."""
        self._editor.setPlainText(text)
        self._status.clear()

    def set_sequence(self, seq: Sequence) -> None:
        """Convert *seq* to DSL text and display it.

        Called automatically when the user switches to the Script tab.
        Any ForLoopAction is rendered as a proper ``for`` block.
        """
        if not seq.actions:
            self.set_text("")
            return
        lines: list[str] = []
        for action in seq.actions:
            try:
                lines.append(action.to_dsl())
            except NotImplementedError:
                pass
        self.set_text("\n".join(lines))

    # ── Validation ─────────────────────────────────────────────────────────

    def _validate(self) -> list[str]:
        text = self.get_text().strip()
        if not text:
            return []
        return ASTValidator().validate(text)

    def _on_validate(self) -> None:
        errors = self._validate()
        if errors:
            self._show_errors(errors)
            return

        # Parse DSL to Sequence for full validation
        text = self.get_text().strip()
        seq = None
        if text:
            try:
                tree = ast.parse(text)
                seq = SequenceBuilder().build(tree)
            except Exception as e:
                self._show_error(f"Parse error: {e}")
                return

        if seq is not None and self._full_validator is not None:
            full_result = self._full_validator(seq)
            if full_result.errors:
                self._show_errors(full_result.errors)
                return
            if full_result.warnings:
                self._show_ok("✓ Validation passed — with warnings:\n" + "\n".join(f"• {w}" for w in full_result.warnings))
                return

        self._show_ok("✓ Validation passed — no errors found.")

    # ── Conversion ─────────────────────────────────────────────────────────

    def _on_convert(self) -> None:
        errors = self._validate()
        if errors:
            self._show_errors(["Fix errors before converting:"] + errors)
            return

        text = self.get_text().strip()
        if not text:
            self._show_error("Nothing to convert (script is empty).")
            return

        try:
            tree = ast.parse(text)
            seq = SequenceBuilder().build(tree)
        except Exception as e:
            self._show_error(f"Parse error: {e}")
            return

        n = len(seq.actions)
        self._show_ok(f"✓ Converted {n} top-level action(s). Switching to Visual tab.")
        self.sequence_changed.emit(seq)

    # ── Status helpers ─────────────────────────────────────────────────────

    def _show_ok(self, message: str) -> None:
        self._status.setStyleSheet("color: #2e7d32;")   # dark green
        self._status.setPlainText(message)

    def _show_error(self, message: str) -> None:
        self._status.setStyleSheet("color: #c62828;")   # dark red
        self._status.setPlainText(message)

    def _show_errors(self, errors: list[str]) -> None:
        self._status.setStyleSheet("color: #c62828;")
        self._status.setPlainText("\n".join(errors))

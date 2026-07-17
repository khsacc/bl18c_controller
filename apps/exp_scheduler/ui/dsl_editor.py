"""
DslEditor — DSL script editor widget for the Experimental Scheduler.

Provides:
  - QPlainTextEdit for DSL text input
  - [Validate] button: runs DslCompiler, reports errors via validation_result
  - [Convert to Visual →] button: validates + parses → emits sequence_changed(Sequence)
  - "Automatically convert to Visual when switching tabs" checkbox: when
    checked (the default), the host window calls convert_to_visual() itself
    on leaving the Script tab, so the button no longer has to be clicked by
    hand. See auto_convert_enabled() / convert_to_visual().
  - set_sequence(): converts a Sequence to DSL text (for Visual → Script sync)

Validation/conversion outcomes are not displayed locally — they are reported
via the validation_result signal so the host window can render them in its
shared "Validation Results" panel (the same one the Visual tab's Validate
button writes to).
"""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..dsl.compiler import DslCompiler
from ..sequence import Sequence
from ..validator.pre_validator import PreCheckResult


class DslEditor(QWidget):
    """
    DSL script editor widget.

    Signals:
        sequence_changed(object) — emitted when the user clicks
            "Convert to Visual" and parsing succeeds.  Carries a Sequence.
        validation_result(object, str) — emitted whenever a Validate/Convert
            outcome needs to be shown.  Carries a PreCheckResult and an
            optional message to use in place of the default "no errors"
            text on success (ignored when the result has errors/warnings).
    """

    sequence_changed = pyqtSignal(object)
    validation_result = pyqtSignal(object, str)

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

        self._chk_auto_convert = QCheckBox("Automatically convert to Visual when switching tabs")
        self._chk_auto_convert.setToolTip(
            "When checked, leaving this tab automatically does what "
            '"Convert to Visual →" does'
        )
        self._chk_auto_convert.setChecked(True)
        bar.addWidget(self._chk_auto_convert)

        bar.addStretch()
        root.addLayout(bar)

        # Text editor
        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText(
            "# Write your DSL experiment script here.\n"
            "#\n"
            "# Example:\n"
            '# wait(duration=5, unit="min")\n'
            '# set_and_wait_pressure(pressure=1.0, unit="MPa", rate=0.05, rate_unit="MPa/min", tol=0.001)\n'
            '# take_xrd(exposure_ms=1000, save=True, prefix="scan")'
        )
        font = self._editor.font()
        font.setFamily("Courier New")
        font.setPointSize(10)
        self._editor.setFont(font)
        root.addWidget(self._editor, stretch=1)

    # ── Public API ─────────────────────────────────────────────────────────

    def get_text(self) -> str:
        """Return the current DSL text."""
        return self._editor.toPlainText()

    def set_text(self, text: str) -> None:
        """Replace the editor content."""
        self._editor.setPlainText(text)

    def auto_convert_enabled(self) -> bool:
        """Return whether "Automatically convert to Visual when switching tabs" is checked."""
        return self._chk_auto_convert.isChecked()

    def convert_to_visual(self) -> None:
        """Run the same conversion as clicking "Convert to Visual →".

        Called by the host window when the user switches away from the
        Script tab and auto-convert is enabled. Does nothing on an empty
        script, since switching tabs without having written anything isn't
        a conversion attempt.
        """
        if not self.get_text().strip():
            return
        self._on_convert()

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

    def _on_validate(self) -> None:
        text = self.get_text().strip()
        if not text:
            self.validation_result.emit(PreCheckResult(), "Validation passed — no errors found")
            return

        result = DslCompiler().compile(text)
        if not result.ok:
            self.validation_result.emit(
                PreCheckResult(errors=[d.message for d in result.diagnostics]), ""
            )
            return

        if self._full_validator is not None:
            # The callback (ExperimentalSchedulerWindow._validate_sequence_from_dsl)
            # renders the result into the shared Validation Results panel itself.
            self._full_validator(result.sequence)
            return

        self.validation_result.emit(PreCheckResult(), "Validation passed — no errors found")

    # ── Conversion ─────────────────────────────────────────────────────────

    def _on_convert(self) -> None:
        text = self.get_text().strip()
        if not text:
            self.validation_result.emit(
                PreCheckResult(errors=["Nothing to convert (script is empty)."]), ""
            )
            return

        result = DslCompiler().compile(text)
        if not result.ok:
            self.validation_result.emit(
                PreCheckResult(errors=[d.message for d in result.diagnostics]), ""
            )
            return

        seq = result.sequence
        n = len(seq.actions)
        self.validation_result.emit(PreCheckResult(), f"Converted {n} action(s) to Visual")
        self.sequence_changed.emit(seq)

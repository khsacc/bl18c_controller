"""
DslEditor — DSL script editor widget for the Experimental Scheduler.

Provides:
  - QPlainTextEdit for DSL text input
  - [Validate] button: runs the host's full validator (compile + device
    preflight), reports the result via validation_result
  - [Convert to Visual →] button: same full validator; on success also
    emits sequence_changed(Sequence)
  - "Automatically convert to Visual when switching tabs" checkbox: when
    checked (the default), the host window calls convert_to_visual() itself
    on leaving the Script tab, so the button no longer has to be clicked by
    hand. See auto_convert_enabled() / convert_to_visual().
  - set_sequence(): converts a Sequence to DSL text (for Visual → Script sync)

Validation/conversion outcomes are not displayed locally — they are reported
via the validation_result signal so the host window can render them in its
shared "Validation Results" panel (the same one the Visual tab's Validate
button writes to).

REORGANISATION_PLAN.md Phase 7 (§7 Phase 7): DslEditor no longer compiles
DSL text itself in the normal (host-connected) path. `set_validator(fn)`
takes a callback `fn(dsl_text: str) -> ValidationReport` that the host runs
end-to-end (apps/exp_scheduler/validation_service.py's `validate_dsl()` —
compile + static checks + live device preflight in one call) and which
itself renders the result and updates the host's validated/Run-enabled
state. Both Validate and Convert call this same callback unconditionally —
including on a syntax error or an empty script — so the host's validated
state is always kept in sync with the latest attempt; DslEditor itself
never short-circuits before reaching the host. (Before Phase 7, DslEditor
ran `DslCompiler().compile()` itself and only invoked the host callback on
a successful compile, so a compile failure or an empty script never
reached the host at all, leaving a stale validated/Run-enabled state
behind.) `DslCompiler` is only used here for the standalone fallback below,
which does not run in production — `ui/scheduler_window.py` is the only
place that constructs a `DslEditor`, and it always calls `set_validator()`.
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
from ..validator.models import Diagnostic, Severity, ValidationPhase, ValidationReport


class DslEditor(QWidget):
    """
    DSL script editor widget.

    Signals:
        sequence_changed(object) — emitted when Convert-to-Visual succeeds
            (compile + full device preflight both passed). Carries a Sequence.
        validation_result(object, str) — emitted whenever a Validate/Convert
            outcome needs to be shown.  Carries a ValidationReport and an
            optional message to use in place of the default "no errors"
            text on success (ignored when the report has errors/warnings).
        text_edited() — emitted whenever the user types in the editor
            (REORGANISATION_PLAN.md §7 Phase 8: "DSL text変更時にcertificateを
            破棄する"). Not emitted for programmatic content changes made via
            set_text()/set_sequence() (see their blockSignals() guard) —
            only those reflect an already-validated Sequence, so invalidating
            on them would discard a still-valid certificate every time the
            host repopulates this editor (e.g. switching to the Script tab).
            The host is expected to connect this to its own
            invalidate-certificate handler.
    """

    sequence_changed = pyqtSignal(object)
    validation_result = pyqtSignal(object, str)
    text_edited = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._validator = None  # callable(dsl_text: str) -> ValidationReport
        self._build_ui()

    def set_validator(self, fn) -> None:
        """Set the host's full validate callback.

        fn(dsl_text: str) -> ValidationReport

        Called for both the Validate and Convert-to-Visual buttons, for
        every attempt (including a syntax error or empty script) — see
        module docstring. The callback is expected to both render the
        result (e.g. into a shared Validation Results panel) and update
        the host's validated/Run-enabled state; DslEditor does not do
        either itself when a validator is set.
        """
        self._validator = fn

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
        self._editor.textChanged.connect(self.text_edited)
        root.addWidget(self._editor, stretch=1)

    # ── Public API ─────────────────────────────────────────────────────────

    def get_text(self) -> str:
        """Return the current DSL text."""
        return self._editor.toPlainText()

    def set_text(self, text: str) -> None:
        """Replace the editor content.

        Blocks the editor's textChanged signal for this programmatic write —
        text_edited() must fire only for actual user keystrokes, not for the
        host repopulating the editor from an already-validated Sequence
        (see the text_edited docstring above).
        """
        self._editor.blockSignals(True)
        try:
            self._editor.setPlainText(text)
        finally:
            self._editor.blockSignals(False)

    def auto_convert_enabled(self) -> bool:
        """Return whether "Automatically convert to Visual when switching tabs" is checked."""
        return self._chk_auto_convert.isChecked()

    def convert_to_visual(self) -> None:
        """Run the same conversion as clicking "Convert to Visual →".

        Called by the host window when the user switches away from the
        Script tab and auto-convert is enabled. Does nothing on an empty
        script, since switching tabs without having written anything isn't
        a conversion attempt (unlike an explicit button click, which always
        runs the full validator — see module docstring).
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
        if self._validator is None:
            self._standalone_compile_feedback(convert=False)
            return
        self._validator(self.get_text())

    # ── Conversion ─────────────────────────────────────────────────────────

    def _on_convert(self) -> None:
        if self._validator is None:
            self._standalone_compile_feedback(convert=True)
            return
        report = self._validator(self.get_text())
        if report.ok and report.sequence is not None:
            self.sequence_changed.emit(report.sequence)

    # ── Standalone fallback (no host validator set) ─────────────────────────

    def _standalone_compile_feedback(self, convert: bool) -> None:
        """Compile-only feedback used only when no host has called
        set_validator() — not exercised in production (see module
        docstring), kept so the widget stays usable on its own (e.g. for
        ad hoc/standalone testing)."""
        text = self.get_text().strip()
        if not text:
            if convert:
                self.validation_result.emit(
                    ValidationReport(diagnostics=[_error_diagnostic(
                        "dsl.empty_script", "Nothing to convert (script is empty).",
                    )]), "",
                )
            else:
                self.validation_result.emit(ValidationReport(), "Validation passed — no errors found")
            return

        result = DslCompiler().compile(text)
        if not result.ok:
            self.validation_result.emit(ValidationReport(diagnostics=result.diagnostics), "")
            return

        if convert:
            n = len(result.sequence.actions)
            self.validation_result.emit(ValidationReport(), f"Converted {n} action(s) to Visual")
            self.sequence_changed.emit(result.sequence)
        else:
            self.validation_result.emit(ValidationReport(), "Validation passed — no errors found")


def _error_diagnostic(code: str, message: str) -> Diagnostic:
    return Diagnostic(Severity.ERROR, code, message, ValidationPhase.COMPILE)

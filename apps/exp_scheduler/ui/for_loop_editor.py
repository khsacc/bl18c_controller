"""
ForLoopEditorDialog — create or edit a ForLoopAction's `var` / `values`.

The loop *body* is not edited here — TimelineWidget manages body steps
(add/edit/delete/reorder) via the existing StepEditorDialog, passing
available_loop_vars=[loop.var] when the selection is inside a loop.
See SPEC.md "Visual Editor での for ループ編集（Phase 2）".
"""
from __future__ import annotations

import keyword

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..actions import Action, ForLoopAction
from ..dsl import ALLOWED_FUNCTIONS
from ..dsl.normalizer import _MAX_RANGE_ELEMENTS


def _no_wheel(widget):
    """Ignore mouse-wheel events on spin boxes (see CLAUDE.md convention)."""
    widget.wheelEvent = lambda event: event.ignore()
    return widget


def _fmt_num(v: float) -> str:
    return f"{v:g}" if v != int(v) else f"{v:.1f}"


class ForLoopEditorDialog(QDialog):
    """
    Dialog for creating or editing a ForLoopAction's `var` and `values`.

    action=None  -> new loop; body starts empty (caller inserts it, then the
                     user adds body steps separately via TimelineWidget)
    action=<obj> -> edit mode; `var`/`values` pre-filled, existing `body` is
                     carried through unchanged (renaming `var` cascades into
                     the body's loop-variable references — see get_action())
    """

    def __init__(self, action: ForLoopAction | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Loop" if action is not None else "Add Loop")
        self.setMinimumWidth(440)

        self._original_var: str | None = action.var if action is not None else None
        self._body: list[Action] = list(action.body) if action is not None else []
        self._built_action: ForLoopAction | None = None

        self._build_ui()

        if action is not None:
            self._var_edit.setText(action.var)
            self._values_edit.setText(", ".join(_fmt_num(v) for v in action.values))
        self._update_validity()

    # ── UI construction ────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        form = QFormLayout()
        self._var_edit = QLineEdit()
        self._var_edit.setPlaceholderText("e.g. p")
        form.addRow("Variable name:", self._var_edit)

        self._values_edit = QLineEdit()
        self._values_edit.setPlaceholderText("e.g. 1.0, 2.0, 3.0, 4.0, 5.0")
        form.addRow("Values:", self._values_edit)
        root.addLayout(form)

        self._var_error = self._error_label()
        root.addWidget(self._var_error)
        self._values_error = self._error_label()
        root.addWidget(self._values_error)

        root.addWidget(QLabel("Generate a range (writes into Values above):"))
        root.addWidget(self._build_generate_row())

        self._var_edit.textChanged.connect(self._update_validity)
        self._values_edit.textChanged.connect(self._update_validity)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    @staticmethod
    def _error_label() -> QLabel:
        lbl = QLabel()
        lbl.setStyleSheet("color: #c0392b;")
        lbl.setWordWrap(True)
        lbl.setVisible(False)
        return lbl

    def _build_generate_row(self) -> QWidget:
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)

        self._gen_start = _no_wheel(QDoubleSpinBox())
        self._gen_start.setRange(-1_000_000.0, 1_000_000.0)
        self._gen_start.setDecimals(6)
        self._gen_stop = _no_wheel(QDoubleSpinBox())
        self._gen_stop.setRange(-1_000_000.0, 1_000_000.0)
        self._gen_stop.setDecimals(6)
        self._gen_stop.setValue(1.0)
        self._gen_step = _no_wheel(QDoubleSpinBox())
        self._gen_step.setRange(-1_000_000.0, 1_000_000.0)
        self._gen_step.setDecimals(6)
        self._gen_step.setValue(0.1)

        hl.addWidget(QLabel("start"))
        hl.addWidget(self._gen_start)
        hl.addWidget(QLabel("stop"))
        hl.addWidget(self._gen_stop)
        hl.addWidget(QLabel("step"))
        hl.addWidget(self._gen_step)
        gen_btn = QPushButton("Generate →")
        gen_btn.clicked.connect(self._on_generate)
        hl.addWidget(gen_btn)
        hl.addStretch()
        return row

    # ── "Generate range" helper ─────────────────────────────────────────

    def _on_generate(self) -> None:
        start = self._gen_start.value()
        stop = self._gen_stop.value()
        step = self._gen_step.value()
        if step == 0:
            self._show_values_error("step must not be 0")
            return

        values: list[float] = []
        v = start
        tol = abs(step) * 1e-9
        while (step > 0 and v <= stop + tol) or (step < 0 and v >= stop - tol):
            values.append(round(v, 6))
            v += step
            if len(values) > _MAX_RANGE_ELEMENTS:
                break

        if not values:
            self._show_values_error("Range produced no points — check start/stop/step.")
            return
        if len(values) > _MAX_RANGE_ELEMENTS:
            self._show_values_error(
                f"Generated range has more than {_MAX_RANGE_ELEMENTS} points "
                "— narrow the range or increase the step."
            )
            return

        self._values_edit.setText(", ".join(_fmt_num(x) for x in values))

    def _show_values_error(self, message: str) -> None:
        self._values_error.setText(message)
        self._values_error.setVisible(True)

    # ── validation ──────────────────────────────────────────────────────

    def _parse_values(self) -> list[float] | None:
        text = self._values_edit.text().strip()
        if not text:
            return None
        tokens = [t.strip() for t in text.split(",")]
        if any(not t for t in tokens):
            return None
        try:
            values = [float(t) for t in tokens]
        except ValueError:
            return None
        if not values or len(values) > _MAX_RANGE_ELEMENTS:
            return None
        return values

    def _validate_var(self) -> str | None:
        """Return an error message, or None if the variable name is valid."""
        name = self._var_edit.text().strip()
        if not name:
            return "Variable name is required."
        if not name.isidentifier():
            return (
                "Variable name must be a valid identifier "
                "(letters, digits, underscore; cannot start with a digit)."
            )
        if keyword.iskeyword(name):
            return f"{name!r} is a Python reserved word."
        if name in ALLOWED_FUNCTIONS:
            return f"{name!r} collides with a DSL function name — choose another name."
        return None

    def _update_validity(self, *_args) -> None:
        var_err = self._validate_var()
        self._var_error.setText(var_err or "")
        self._var_error.setVisible(bool(var_err))

        values = self._parse_values()
        if values is None:
            self._show_values_error(
                f"Enter a comma-separated list of numbers (1–{_MAX_RANGE_ELEMENTS})."
            )
        else:
            self._values_error.setVisible(False)

        self._ok_btn.setEnabled(var_err is None and values is not None)

    # ── OK handler ─────────────────────────────────────────────────────

    def _on_ok(self) -> None:
        var = self._var_edit.text().strip()
        values = self._parse_values()
        if values is None or self._validate_var() is not None:
            return

        body = self._body
        if self._original_var is not None and var != self._original_var:
            from ..actions import rename_loop_var_refs
            rename_loop_var_refs(body, self._original_var, var)

        self._built_action = ForLoopAction(var=var, values=values, body=body)
        self.accept()

    # ── Public API ─────────────────────────────────────────────────────

    def get_action(self) -> ForLoopAction | None:
        """Return the constructed/edited ForLoopAction after OK, or None."""
        return self._built_action

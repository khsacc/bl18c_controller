"""Per-ruby zero-pressure references (λ₀ and its measurement temperature T₀)."""
from __future__ import annotations

import math

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from PyQt6.QtCore import Qt

from ..ruby_pressure import LAMBDA_0_RANGE_NM, T0_RANGE_K


def _format(value) -> str:
    return "" if value is None else f"{value:g}"


class RubyReferenceDialog(QDialog):
    """Edit λ₀ (nm) and T₀ (K) for each registered ruby position.

    Each ruby keeps its own reference; empty cells mean "not registered".
    T₀ is only needed with temperature correction, so its column is
    read-only when correction is off (values already entered are kept).
    """

    def __init__(self, names, references: dict, *, temperature_correction: bool,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ruby zero-pressure references")
        self.references = {name: dict(values) for name, values in references.items()}
        layout = QVBoxLayout(self)
        note = QLabel(
            "Enter the R1 peak position λ₀ measured at zero pressure for each ruby, "
            "and the temperature T₀ at which it was measured. Leave a cell empty if "
            "the value has not been measured."
            + ("" if temperature_correction else
               "\nT₀ is used only with temperature correction (currently off).")
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self._names = list(names)
        self._table = QTableWidget(len(self._names), 3)
        self._table.setHorizontalHeaderLabels(["Ruby", "λ₀ (nm)", "T₀ (K)"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, name in enumerate(self._names):
            values = self.references.get(name) or {}
            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, name_item)
            self._table.setItem(row, 1, QTableWidgetItem(_format(values.get("lambda_0_nm"))))
            t0_item = QTableWidgetItem(_format(values.get("t0_k")))
            if not temperature_correction:
                t0_item.setFlags(t0_item.flags() & ~Qt.ItemFlag.ItemIsEditable
                                 & ~Qt.ItemFlag.ItemIsEnabled)
            self._table.setItem(row, 2, t0_item)
        layout.addWidget(self._table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(420, (120 + 30 * max(1, len(self._names))) * 3 // 2)

    def _cell_value(self, row: int, column: int, what: str, limits) -> float | None:
        item = self._table.item(row, column)
        text = item.text().strip() if item is not None else ""
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"{what} of {self._names[row]!r} is not a number: {text!r}")
        if not math.isfinite(value) or not limits[0] <= value <= limits[1]:
            raise ValueError(
                f"{what} of {self._names[row]!r} must be {limits[0]:g}–{limits[1]:g}"
            )
        return value

    def parsed_references(self) -> dict:
        result = {}
        for row, name in enumerate(self._names):
            lambda_0 = self._cell_value(row, 1, "λ₀ (nm)", LAMBDA_0_RANGE_NM)
            t0 = self._cell_value(row, 2, "T₀ (K)", T0_RANGE_K)
            if lambda_0 is None and t0 is None:
                continue
            result[name] = {"lambda_0_nm": lambda_0, "t0_k": t0}
        return result

    def _on_accept(self) -> None:
        try:
            self.references = self.parsed_references()
        except ValueError as exc:
            QMessageBox.warning(self, "Ruby zero-pressure references", str(exc))
            return
        self.accept()

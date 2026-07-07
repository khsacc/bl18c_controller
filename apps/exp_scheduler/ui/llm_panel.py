"""
AI Assist panel — the third tab in ExperimentalSchedulerWindow.

Layout
------
Top:   connection settings (model selector, URL, [Test Connection], status)
Left:  conversation history + input line + [Send] / [Clear Chat]
Right: generated DSL preview + validation status + [Apply to Timeline]
       + [Explain Current DSL] button at the bottom

Signals
-------
sequence_applied(Sequence)
    Emitted when the user clicks [Apply to Timeline] with a valid DSL loaded.
    The scheduler window connects this to update both Timeline and Script tabs.
"""
from __future__ import annotations

import ast

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..dsl.parser import SequenceBuilder
from ..llm.client import OllamaChatWorker, OllamaConnectionWorker
from ..llm.prompt_builder import build_explain_prompt
from ..llm.session import LlmSession
from ..sequence import Sequence

_MAX_SELFFIX_RETRIES: int = 3

_DEFAULT_URL: str = "http://localhost:11434"
_PREFERRED_MODELS: list[str] = [
    "qwen2.5-coder:7b",
    "qwen3-coder:30b",
]


# ── Explain dialog ─────────────────────────────────────────────────────────────


class _ExplainDialog(QDialog):
    """Modal dialog that fetches and shows a Japanese explanation of DSL."""

    def __init__(
        self,
        dsl_text: str,
        base_url: str,
        model: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("DSL Explanation")
        self.resize(600, 400)
        self._dsl_text = dsl_text
        self._base_url = base_url
        self._model = model
        self._worker: OllamaChatWorker | None = None

        layout = QVBoxLayout(self)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setPlaceholderText("Fetching explanation…")
        layout.addWidget(self._text)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._fetch()

    def _fetch(self) -> None:
        system = build_explain_prompt().render()
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"次のDSLを説明してください:\n```python\n{self._dsl_text}\n```",
            },
        ]
        self._worker = OllamaChatWorker(
            base_url=self._base_url,
            model=self._model,
            messages=messages,
            parent=self,
        )
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_finished(self, response: str) -> None:
        self._text.setPlainText(response)

    def _on_error(self, msg: str) -> None:
        self._text.setPlainText(f"Error: {msg}")

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(2000)
        super().closeEvent(event)


# ── Main panel ────────────────────────────────────────────────────────────────


class LlmPanel(QWidget):
    """AI Assist tab widget.

    Parameters
    ----------
    get_dsl_fn : callable or None
        Zero-argument callable that returns the current DSL text from the
        Script (DslEditor) tab.  Used by the Explain button.
    """

    sequence_applied: pyqtSignal = pyqtSignal(object)  # Sequence

    def __init__(self, get_dsl_fn=None, parent=None) -> None:
        super().__init__(parent)
        self._session = LlmSession()
        self._worker: OllamaChatWorker | None = None
        self._conn_worker: OllamaConnectionWorker | None = None
        self._selffix_count: int = 0
        self._pending_dsl: str | None = None   # last valid DSL ready to apply
        self._get_dsl_fn = get_dsl_fn
        self._setup_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        root.addWidget(self._make_connection_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._make_conversation_pane())
        splitter.addWidget(self._make_dsl_pane())
        splitter.setSizes([500, 400])
        root.addWidget(splitter, stretch=1)

    def _make_connection_bar(self) -> QGroupBox:
        group = QGroupBox("Connection")
        layout = QHBoxLayout(group)
        layout.setSpacing(6)

        layout.addWidget(QLabel("Model:"))
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        for m in _PREFERRED_MODELS:
            self._model_combo.addItem(m)
        self._model_combo.setMinimumWidth(180)
        layout.addWidget(self._model_combo)

        layout.addWidget(QLabel("URL:"))
        self._url_edit = QLineEdit(_DEFAULT_URL)
        self._url_edit.setMinimumWidth(200)
        layout.addWidget(self._url_edit)

        self._btn_test = QPushButton("Test Connection")
        self._btn_test.clicked.connect(self._on_test_connection)
        layout.addWidget(self._btn_test)

        self._conn_status = QLabel("Not connected")
        self._conn_status.setStyleSheet("color: gray;")
        layout.addWidget(self._conn_status)
        layout.addStretch()

        return group

    def _make_conversation_pane(self) -> QGroupBox:
        group = QGroupBox("Conversation")
        layout = QVBoxLayout(group)

        self._chat_view = QTextEdit()
        self._chat_view.setReadOnly(True)
        self._chat_view.setFont(QFont("monospace", 9))
        layout.addWidget(self._chat_view, stretch=1)

        input_row = QHBoxLayout()
        self._input_edit = QLineEdit()
        self._input_edit.setPlaceholderText("Type your request here…")
        self._input_edit.returnPressed.connect(self._on_send)
        input_row.addWidget(self._input_edit)

        self._btn_send = QPushButton("Send")
        self._btn_send.clicked.connect(self._on_send)
        input_row.addWidget(self._btn_send)
        layout.addLayout(input_row)

        self._btn_clear = QPushButton("Clear Chat")
        self._btn_clear.clicked.connect(self._on_clear_chat)
        layout.addWidget(self._btn_clear)

        return group

    def _make_dsl_pane(self) -> QGroupBox:
        group = QGroupBox("Generated DSL")
        layout = QVBoxLayout(group)

        self._dsl_preview = QPlainTextEdit()
        self._dsl_preview.setReadOnly(True)
        self._dsl_preview.setFont(QFont("monospace", 9))
        self._dsl_preview.setPlaceholderText("Generated DSL will appear here…")
        layout.addWidget(self._dsl_preview, stretch=1)

        self._validation_label = QLabel("")
        self._validation_label.setWordWrap(True)
        layout.addWidget(self._validation_label)

        self._btn_apply = QPushButton("Apply to Timeline")
        self._btn_apply.setEnabled(False)
        self._btn_apply.clicked.connect(self._on_apply)
        layout.addWidget(self._btn_apply)

        self._btn_explain = QPushButton("Explain Current DSL")
        self._btn_explain.clicked.connect(self._on_explain)
        layout.addWidget(self._btn_explain)

        return group

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------

    def _on_test_connection(self) -> None:
        if self._conn_worker and self._conn_worker.isRunning():
            return
        self._conn_status.setText("Testing…")
        self._conn_status.setStyleSheet("color: gray;")
        self._btn_test.setEnabled(False)

        self._conn_worker = OllamaConnectionWorker(
            base_url=self._url_edit.text().strip(),
            parent=self,
        )
        self._conn_worker.success.connect(self._on_conn_success)
        self._conn_worker.error.connect(self._on_conn_error)
        self._conn_worker.start()

    def _on_conn_success(self, models: list[str]) -> None:
        self._btn_test.setEnabled(True)
        count = len(models)
        self._conn_status.setText(f"● Connected ({count} model{'s' if count != 1 else ''})")
        self._conn_status.setStyleSheet("color: green;")

        current = self._model_combo.currentText()
        self._model_combo.clear()
        # Show preferred models first, then the rest
        ordered: list[str] = []
        for p in _PREFERRED_MODELS:
            if p in models:
                ordered.append(p)
        for m in models:
            if m not in ordered:
                ordered.append(m)
        for m in ordered:
            self._model_combo.addItem(m)

        if current in ordered:
            self._model_combo.setCurrentText(current)
        elif ordered:
            self._model_combo.setCurrentIndex(0)

    def _on_conn_error(self, msg: str) -> None:
        self._btn_test.setEnabled(True)
        self._conn_status.setText(f"✗ {msg}")
        self._conn_status.setStyleSheet("color: red;")

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    def _on_send(self) -> None:
        text = self._input_edit.text().strip()
        if not text or (self._worker and self._worker.isRunning()):
            return
        self._input_edit.clear()
        self._append_conversation("You", text)
        self._set_ui_busy(True)

        messages = self._session.build_messages(text)
        self._start_worker(messages, self._on_chat_finished)

    def _on_chat_finished(self, response: str) -> None:
        self._session.record_assistant_response(response)
        self._append_conversation("AI", response)

        dsl, errors = self._session.try_extract_and_validate(response)
        if dsl:
            self._show_dsl_preview(dsl, valid=True, errors=[])
            self._pending_dsl = dsl
            self._set_ui_busy(False)
        elif errors:
            self._show_dsl_preview(
                self._session.last_dsl or "", valid=False, errors=errors
            )
            raw = self._session._extract_dsl(response) or ""
            self._selffix_count = 0
            self._start_selffix(raw, errors)
        else:
            self._set_ui_busy(False)

    # ------------------------------------------------------------------
    # Self-fix loop
    # ------------------------------------------------------------------

    def _start_selffix(self, dsl_text: str, errors: list[str]) -> None:
        if self._selffix_count >= _MAX_SELFFIX_RETRIES:
            self._append_conversation(
                "System",
                f"Could not produce valid DSL after {_MAX_SELFFIX_RETRIES} retries.\n"
                "Errors:\n" + "\n".join(f"  • {e}" for e in errors),
            )
            self._set_ui_busy(False)
            return

        self._selffix_count += 1
        self._append_conversation(
            "System",
            f"Validation failed ({self._selffix_count}/{_MAX_SELFFIX_RETRIES}). "
            "Asking model to fix…",
        )
        messages = self._session.build_selffix_messages(dsl_text, errors)
        self._start_worker(messages, self._on_selffix_finished)

    def _on_selffix_finished(self, response: str) -> None:
        dsl, errors = self._session.apply_selffix_response(response)
        if dsl:
            self._append_conversation("AI", "(Fixed DSL generated)")
            self._show_dsl_preview(dsl, valid=True, errors=[])
            self._pending_dsl = dsl
            self._set_ui_busy(False)
        elif errors:
            raw = self._session._extract_dsl(response) or ""
            self._start_selffix(raw, errors)
        else:
            self._append_conversation(
                "System", "Self-fix response contained no code block."
            )
            self._set_ui_busy(False)

    # ------------------------------------------------------------------
    # Apply to timeline
    # ------------------------------------------------------------------

    def _on_apply(self) -> None:
        if not self._pending_dsl:
            return
        try:
            tree = ast.parse(self._pending_dsl)
            sequence = SequenceBuilder().build(tree)
        except Exception as exc:
            QMessageBox.critical(self, "Parse Error", str(exc))
            return
        self.sequence_applied.emit(sequence)
        self._append_conversation("System", "Sequence applied to timeline.")

    # ------------------------------------------------------------------
    # Explain current DSL
    # ------------------------------------------------------------------

    def _on_explain(self) -> None:
        # Prefer the DSL in the preview; fall back to DslEditor content.
        dsl = self._pending_dsl
        if not dsl and self._get_dsl_fn:
            dsl = self._get_dsl_fn().strip()
        if not dsl:
            QMessageBox.information(
                self,
                "No DSL",
                "No DSL to explain. Generate a DSL first or switch to the "
                "Script tab and write one.",
            )
            return
        dialog = _ExplainDialog(
            dsl_text=dsl,
            base_url=self._url_edit.text().strip(),
            model=self._model_combo.currentText(),
            parent=self,
        )
        dialog.exec()

    # ------------------------------------------------------------------
    # Clear chat
    # ------------------------------------------------------------------

    def _on_clear_chat(self) -> None:
        self._session.reset()
        self._chat_view.clear()
        self._dsl_preview.clear()
        self._validation_label.setText("")
        self._pending_dsl = None
        self._btn_apply.setEnabled(False)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_worker(
        self,
        messages: list[dict],
        on_finished,
    ) -> None:
        self._worker = OllamaChatWorker(
            base_url=self._url_edit.text().strip(),
            model=self._model_combo.currentText(),
            messages=messages,
            parent=self,
        )
        self._worker.finished.connect(on_finished)
        self._worker.error.connect(self._on_worker_error)
        self._worker.start()

    def _on_worker_error(self, msg: str) -> None:
        self._append_conversation("System", f"Error: {msg}")
        self._set_ui_busy(False)

    def _append_conversation(self, role: str, text: str) -> None:
        cursor = self._chat_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        fmt_role = QTextCharFormat()
        fmt_body = QTextCharFormat()

        if role == "You":
            fmt_role.setForeground(QColor("#1565C0"))
            fmt_role.setFontWeight(700)
        elif role == "AI":
            fmt_role.setForeground(QColor("#2E7D32"))
            fmt_role.setFontWeight(700)
        else:
            fmt_role.setForeground(QColor("#888888"))
            fmt_role.setFontItalic(True)

        if not self._chat_view.toPlainText():
            pass
        else:
            cursor.insertText("\n\n", QTextCharFormat())

        cursor.insertText(f"[{role}] ", fmt_role)
        cursor.insertText(text, fmt_body)
        self._chat_view.setTextCursor(cursor)
        self._chat_view.ensureCursorVisible()

    def _set_ui_busy(self, busy: bool) -> None:
        self._btn_send.setEnabled(not busy)
        self._input_edit.setEnabled(not busy)
        self._btn_apply.setEnabled(not busy and self._pending_dsl is not None)
        if busy:
            self._btn_send.setText("…")
        else:
            self._btn_send.setText("Send")

    def _show_dsl_preview(
        self, dsl_text: str, valid: bool, errors: list[str]
    ) -> None:
        self._dsl_preview.setPlainText(dsl_text)
        if valid:
            self._validation_label.setText("✓ Validation OK")
            self._validation_label.setStyleSheet("color: green;")
            self._btn_apply.setEnabled(True)
        else:
            error_str = "\n".join(f"• {e}" for e in errors[:5])
            self._validation_label.setText(f"✗ Validation failed:\n{error_str}")
            self._validation_label.setStyleSheet("color: red;")
            self._btn_apply.setEnabled(False)

    def closeEvent(self, event) -> None:
        for w in (self._worker, self._conn_worker):
            if w and w.isRunning():
                w.quit()
                w.wait(2000)
        super().closeEvent(event)

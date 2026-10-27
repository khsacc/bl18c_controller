"""
TimelineWidget — visual sequence timeline for the Experimental Scheduler.

Two views share the widget (QStackedWidget):
  - Editor — the loop-structured tree used for Add Step / Edit / etc.
  - Run log — shown when a run starts (show_run_plan()). It lists the
    runner's loop-unrolled plan (run_plan.py), one row per execution, so
    every loop iteration keeps its own Started / Finished / Saved-file
    record. Row i is flat_index i of SequenceRunner's signals.

Example — sequence: [action_a, for p in [0.5, 1.0]: [action_b, action_c], action_d]
  run-log row 0 → action_a
  run-log row 1 → action_b  [p=0.5 (1/2)]
  run-log row 2 → action_c  [p=0.5 (1/2)]
  run-log row 3 → action_b  [p=1.0 (2/2)]
  run-log row 4 → action_c  [p=1.0 (2/2)]
  run-log row 5 → action_d

The run log stays available after the run (toggle button) until the next
validation, run, or sequence replacement clears it.

Loop editing (Phase 2, see SPEC.md "Visual Editor での for ループ編集"):
  - "+ Add Loop" creates a new top-level ForLoopAction via ForLoopEditorDialog
    (var/values only — nesting is not supported from Visual).
  - The existing "+ Add Step" / "Edit" / "Delete" / "▲ Up" / "▼ Down" buttons
    become context-aware: when the current selection is a loop header or a
    loop-body child, they operate on that loop's body instead of the
    top-level sequence. `_loop_body_insert_index()` is the single place that
    decides this.
  - A ForLoopAction's body is NOT cached authoritatively on the Action object
    kept in `_item_to_action` — the tree is the source of truth for body
    contents once a loop is on screen. `_rebuild_loop_action()` reconstructs
    a fresh ForLoopAction (var/values from the cached object, body from the
    tree's current children) wherever the body might have been edited since
    the loop was added: get_sequence(), editing the loop header, and
    refreshing the header's "(N steps, M loops)" label.
  - A nested ForLoopAction (only possible via a DSL sequence converted to
    Visual — Visual itself never creates nesting) is shown as a single
    opaque, non-editable placeholder row.

Clipboard and undo:
  - The editor tree allows multiple selection. Copy (Ctrl+C) takes every
    selected step in tree order; a selected loop header copies the whole
    loop with its current body (children of a selected loop are not copied
    twice). Paste ↓ (Ctrl+V) / Paste ↑ insert the whole block.
  - Undo (Ctrl+Z) restores a snapshot of the sequence taken before each
    edit (add, edit, delete, move, paste, drag, and set_sequence() when it
    changes the content). There is no redo.
"""
from __future__ import annotations

import copy
import html
import os
from pathlib import Path

from PyQt6.QtCore import QEvent, QItemSelectionModel, QMarginsF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QKeySequence,
    QPageLayout,
    QPageSize,
    QTextDocument,
)
from PyQt6.QtPrintSupport import QPrinter
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..actions import (
    Action,
    AlignToReferenceAction,
    AllHeatersOffAction,
    FollowSampleAction,
    ForLoopAction,
    FpdOutMicroscopeInAction,
    LOOP_VAR_FIELDS,
    MicroscopeOutFpdInAction,
    SaveReferenceImageAction,
    SaveSnapshotAction,
    SetControlModeAction,
    SetHeaterAction,
    SetPressureAction,
    SetTemperatureAction,
    SetAndWaitTemperatureAction,
    StageAction,
    StartFollowingAction,
    StopFollowingAction,
    TakeDarkAction,
    TakeSpectrumAction,
    TakeXrdAction,
    WaitPressureAction,
    WaitTemperatureAction,
    action_loop_var_ref,
)
from ..sequence import Sequence


# ── Color palette ──────────────────────────────────────────────────────────────

_COLORS: dict[str, str] = {
    "stage":    "#eef6ff",   # light blue
    "pace5000": "#fff8ec",   # light orange
    "lakeshore":"#fdeef3",   # light pink
    "xrd":      "#f1f8f1",   # light green
    "spectrum": "#ecfafc",   # light cyan
    "camera":   "#f8eff9",   # light purple
    "general":  "#f9f9f9",   # light gray
}

_COLOR_RUNNING = "#fff176"   # yellow  — currently executing
_COLOR_DONE    = "#c8e6c9"   # light green — completed

_UNDO_LIMIT = 100


def _device_key(action: Action) -> str:
    if isinstance(action, (StageAction, MicroscopeOutFpdInAction, FpdOutMicroscopeInAction)):
        return "stage"
    if isinstance(action, (SetPressureAction, WaitPressureAction, SetControlModeAction)):
        return "pace5000"
    if isinstance(action, (SetTemperatureAction, WaitTemperatureAction, SetAndWaitTemperatureAction,
                           SetHeaterAction, AllHeatersOffAction)):
        return "lakeshore"
    if isinstance(action, (TakeDarkAction, TakeXrdAction)):
        return "xrd"
    if isinstance(action, TakeSpectrumAction):
        return "spectrum"
    if isinstance(action, (SaveReferenceImageAction, SaveSnapshotAction, StartFollowingAction,
                           StopFollowingAction, FollowSampleAction,
                           AlignToReferenceAction)):
        return "camera"
    return "general"


def _base_color(action: Action) -> QColor:
    return QColor(_COLORS[_device_key(action)])


def _nested_loop_label(action: ForLoopAction) -> str:
    return f"[Caution] Nested loop — edit via Script tab: {action.describe()}"


def _same_actions(a: list, b: list) -> bool:
    """True if both action lists serialise identically (False if unsure)."""
    try:
        return [x.to_dict() for x in a] == [y.to_dict() for y in b]
    except Exception:
        return False


# ── QTreeWidget subclass ───────────────────────────────────────────────────────

class _SequenceTree(QTreeWidget):
    """
    QTreeWidget that restricts InternalMove drops to top-level reordering.
    Dropping an item OnItem (which would make it a child) is blocked.

    Copy / Paste / Undo key presses are claimed here (ShortcutOverride) so
    that a window-level shortcut or QTreeWidget's own text copy cannot take
    them while the tree has focus.
    """

    about_to_reorder = pyqtSignal()
    rows_reordered = pyqtSignal()
    copy_requested = pyqtSignal()
    paste_requested = pyqtSignal()
    undo_requested = pyqtSignal()

    def _key_signal(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            return self.copy_requested
        if event.matches(QKeySequence.StandardKey.Paste):
            return self.paste_requested
        if event.matches(QKeySequence.StandardKey.Undo):
            return self.undo_requested
        return None

    def event(self, event) -> bool:
        if (event.type() == QEvent.Type.ShortcutOverride
                and self._key_signal(event) is not None):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        signal = self._key_signal(event)
        if signal is not None:
            signal.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def dropEvent(self, event) -> None:
        if (self.dropIndicatorPosition()
                == QAbstractItemView.DropIndicatorPosition.OnItem):
            event.ignore()
            return
        self.about_to_reorder.emit()
        super().dropEvent(event)
        self.rows_reordered.emit()


# ── Main widget ────────────────────────────────────────────────────────────────

class TimelineWidget(QWidget):
    """
    Visual timeline for a Sequence.

    Signals:
        sequence_changed — emitted whenever the user edits the sequence
                           (add / delete / reorder). Consumers should call
                           get_sequence() to retrieve the updated state.
    """

    sequence_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.spectrum_position_names = lambda: ()
        self.global_positions = lambda: None

        # id(item) → (item, Action)  — QTreeWidgetItem is not hashable in PyQt6
        self._item_to_action: dict[int, tuple[QTreeWidgetItem, Action]] = {}
        self._clipboard: list[Action] = []
        # Each entry: (deep-copied top-level actions, current-item path).
        self._undo_stack: list[tuple[list[Action], tuple[int, ...] | None]] = []
        self._drag_snapshot = None

        # Run log: row i <-> SequenceRunner flat_index i (see run_plan.py).
        self._run_items: list[QTreeWidgetItem] = []
        self._run_actions: list[Action] = []
        self._run_files: dict[int, list[str]] = {}
        self._highlighted_run_row: int | None = None
        self._editing_allowed = True

        self._build_ui()

        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_app_focus_changed)

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        root.addLayout(self._make_toolbar())
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._make_tree())
        self._stack.addWidget(self._make_run_tree())
        root.addWidget(self._stack)

    def _make_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()

        self._btn_add = QPushButton("+ Add Step")
        self._btn_add.clicked.connect(self._on_add)
        bar.addWidget(self._btn_add)

        self._btn_add_loop = QPushButton("+ Add Loop")
        self._btn_add_loop.clicked.connect(self._on_add_loop)
        bar.addWidget(self._btn_add_loop)

        self._btn_edit = QPushButton("Edit")
        self._btn_edit.clicked.connect(self._on_edit)
        bar.addWidget(self._btn_edit)

        self._btn_delete = QPushButton("Delete")
        self._btn_delete.clicked.connect(self._on_delete)
        bar.addWidget(self._btn_delete)

        self._btn_copy = QPushButton("Copy")
        self._btn_copy.setToolTip(
            "Copy the selected steps (Ctrl+C). Select a loop header to copy "
            "the whole loop. Ctrl/Shift-click selects several steps."
        )
        self._btn_copy.clicked.connect(self._on_copy)
        bar.addWidget(self._btn_copy)

        self._btn_paste_above = QPushButton("Paste ↑")
        self._btn_paste_above.setToolTip("Paste above the selection")
        self._btn_paste_above.clicked.connect(self._on_paste_above)
        bar.addWidget(self._btn_paste_above)

        self._btn_paste_below = QPushButton("Paste ↓")
        self._btn_paste_below.setToolTip("Paste below the selection (Ctrl+V)")
        self._btn_paste_below.clicked.connect(self._on_paste_below)
        bar.addWidget(self._btn_paste_below)

        self._btn_undo = QPushButton("Undo")
        self._btn_undo.setToolTip("Undo the last edit (Ctrl+Z)")
        self._btn_undo.clicked.connect(self._on_undo)
        bar.addWidget(self._btn_undo)

        self._btn_up = QPushButton("▲  Up")
        self._btn_up.clicked.connect(self._on_move_up)
        bar.addWidget(self._btn_up)

        self._btn_down = QPushButton("▼  Down")
        self._btn_down.clicked.connect(self._on_move_down)
        bar.addWidget(self._btn_down)

        bar.addStretch()

        self._context_label = QLabel()
        self._context_label.setStyleSheet("color: #555; font-style: italic;")
        self._context_label.setVisible(False)
        bar.addWidget(self._context_label)

        self._btn_toggle_view = QPushButton("Show editor")
        self._btn_toggle_view.setToolTip(
            "Switch between the run log (loops expanded) and the sequence editor"
        )
        self._btn_toggle_view.clicked.connect(self._on_toggle_view)
        self._btn_toggle_view.setVisible(False)
        bar.addWidget(self._btn_toggle_view)

        return bar

    def _make_tree(self) -> _SequenceTree:
        self._tree = _SequenceTree(self)
        self._tree.setColumnCount(1)
        self._tree.setHeaderLabels(["Step"])
        self._tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._tree.about_to_reorder.connect(self._on_about_to_reorder)
        self._tree.rows_reordered.connect(self._on_rows_reordered)
        self._tree.copy_requested.connect(self._on_copy)
        self._tree.paste_requested.connect(self._on_paste_below)
        self._tree.undo_requested.connect(self._on_undo)
        self._tree.itemSelectionChanged.connect(self._update_context_label)
        return self._tree

    def _make_run_tree(self) -> QTreeWidget:
        self._run_tree = QTreeWidget(self)
        self._run_tree.setColumnCount(5)
        self._run_tree.setHeaderLabels(["#", "Step", "Started", "Finished", "Saved file"])
        self._run_tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((50, 340, 155, 155, 220)):
            self._run_tree.setColumnWidth(column, width)
        self._run_tree.setRootIsDecorated(False)
        self._run_tree.setUniformRowHeights(True)
        self._run_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        return self._run_tree

    # ── Public API ─────────────────────────────────────────────────────────

    def set_sequence(self, sequence: Sequence) -> None:
        """Replace the displayed sequence. Clears any run log.

        If the content changes, the previous content is kept on the undo
        stack (e.g. Load, Clear All, Convert to Visual can be undone)."""
        before = self._snapshot()
        if not _same_actions(before[0], sequence.actions):
            self._push_undo(before)
        self._tree.clear()
        self._item_to_action.clear()
        self.clear_run_progress()
        self._populate(sequence.actions)

    def slack_screenshot_widget(self) -> QWidget:
        """Return the currently displayed step table; no scrolling or view switch."""
        return self._stack.currentWidget()

    def set_editing_enabled(self, enabled: bool) -> None:
        """Keep the tree readable while a run is in progress."""
        self._editing_allowed = enabled
        self._sync_editing_controls()

    def _sync_editing_controls(self) -> None:
        # The edit buttons act on the editor tree, so they are also disabled
        # while the run log is the visible view.
        enabled = self._can_edit()
        for button in (self._btn_add, self._btn_add_loop, self._btn_edit,
                       self._btn_delete, self._btn_copy, self._btn_paste_above,
                       self._btn_paste_below, self._btn_up, self._btn_down):
            button.setEnabled(enabled)
        self._btn_undo.setEnabled(enabled and bool(self._undo_stack))
        self._tree.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove if enabled
            else QAbstractItemView.DragDropMode.NoDragDrop
        )

    def show_run_plan(self, steps: list) -> None:
        """Replace the run log with `steps` (run_plan.RunStep list, i.e.
        SequenceRunner.run_plan) and switch to the run-log view."""
        self._clear_run_log()
        self._run_tree.setUpdatesEnabled(False)
        try:
            items = []
            for step in steps:
                description = step.describe()
                item = QTreeWidgetItem([str(step.index + 1), description, "", "", ""])
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                item.setToolTip(1, description)
                for column in range(self._run_tree.columnCount()):
                    item.setBackground(column, QBrush(_base_color(step.action)))
                items.append(item)
                self._run_actions.append(step.action)
            self._run_tree.addTopLevelItems(items)
            self._run_items = items
        finally:
            self._run_tree.setUpdatesEnabled(True)
        self._btn_toggle_view.setVisible(True)
        self._set_view(1)

    def clear_run_progress(self) -> None:
        """Discard the run log and return to the editor view."""
        self.clear_highlights()
        self._clear_run_log()
        self._btn_toggle_view.setVisible(False)
        self._set_view(0)

    def _clear_run_log(self) -> None:
        self._run_tree.clear()
        self._run_items = []
        self._run_actions = []
        self._run_files.clear()
        self._highlighted_run_row = None

    def _set_view(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._btn_toggle_view.setText("Show run log" if index == 0 else "Show editor")
        if index == 1:
            self._context_label.setVisible(False)
        else:
            self._update_context_label()
        self._sync_editing_controls()

    def _on_toggle_view(self) -> None:
        self._set_view(1 if self._stack.currentIndex() == 0 else 0)

    def _run_item(self, flat_index: int) -> QTreeWidgetItem | None:
        if 0 <= flat_index < len(self._run_items):
            return self._run_items[flat_index]
        return None

    def record_step_started(self, flat_index: int, timestamp: str) -> None:
        item = self._run_item(flat_index)
        if item is not None:
            item.setText(2, timestamp)

    def record_step_finished(self, flat_index: int, timestamp: str) -> None:
        item = self._run_item(flat_index)
        if item is not None:
            item.setText(3, timestamp)

    def record_file_saved(self, flat_index: int, path: str) -> None:
        """One step may save several files (e.g. spectra at several sample
        positions) — all are kept: names in the cell, full paths in the tooltip."""
        item = self._run_item(flat_index)
        if item is None:
            return
        files = self._run_files.setdefault(flat_index, [])
        files.append(path)
        item.setText(4, ", ".join(Path(f).name for f in files))
        item.setToolTip(4, "\n".join(files))

    def save_run_log_pdf(self, path: Path, title: str) -> None:
        """Print the displayed run-log rows to a paginated A4 PDF."""
        headings = (("#", 5), ("Step", 35), ("Started", 20),
                    ("Finished", 20), ("Saved file", 20))
        rows = []
        for index, item in enumerate(self._run_items):
            cells = [html.escape(item.text(column)) for column in range(4)]
            files = self._run_files.get(index, [])
            cells.append("<br/>".join(html.escape(Path(file).name) for file in files))
            colour = item.background(0).color().name()
            rows.append(
                f'<tr style="background-color: {colour}">'
                + "".join(f"<td>{cell}</td>" for cell in cells)
                + "</tr>"
            )

        document = QTextDocument(self)
        font = document.defaultFont()
        # The explicit page size below uses printer points, so one document
        # pixel renders as one PDF point.
        font.setPixelSize(6)
        document.setDefaultFont(font)
        document.setHtml(
            '<html><head><style>'
            'body { font-family: sans-serif; font-size: 6px; }'
            'table { border-collapse: collapse; width: 100%; }'
            'th, td { border: 1px solid #777; padding: 1px; vertical-align: top; }'
            'th { background-color: #ddd; }'
            '</style></head><body>'
            f'<p style="font-size: 6px; font-weight: bold; margin-bottom: 2px">Run log — {html.escape(title)}</p>'
            '<table cellspacing="0" cellpadding="1" width="100%">'
            '<thead><tr>'
            + "".join(f'<th width="{width}%">{heading}</th>'
                      for heading, width in headings)
            + '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></body></html>'
        )

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        printer.setPageOrientation(QPageLayout.Orientation.Portrait)
        printer.setPageMargins(QMarginsF(5, 5, 5, 5), QPageLayout.Unit.Millimeter)
        # QTextDocument adds a large default print margin unless paginated explicitly.
        document.setPageSize(printer.pageRect(QPrinter.Unit.Point).size())
        temporary = path.with_name(f"{path.stem}.tmp.pdf")
        printer.setOutputFileName(str(temporary))
        try:
            document.print(printer)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise OSError("Qt did not create the run log PDF")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def get_sequence(self) -> Sequence:
        """Return a Sequence that reflects the current tree order, including
        any body edits made to ForLoopAction items (see _rebuild_loop_action)."""
        actions: list[Action] = []
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            action = self._get_action(item)
            if action is None:
                continue
            if isinstance(action, ForLoopAction):
                actions.append(self._rebuild_loop_action(item, action))
            else:
                actions.append(action)
        return Sequence(actions=actions)

    def highlight_step(self, flat_index: int) -> None:
        """Highlight the run-log row at flat_index (yellow); the previously
        highlighted row reverts to its base device colour."""
        previous = self._highlighted_run_row
        if previous is not None and previous != flat_index:
            self._paint_run_row(previous, _base_color(self._run_actions[previous]))
        self._highlighted_run_row = None
        item = self._run_item(flat_index)
        if item is None:
            return
        self._paint_run_row(flat_index, QColor(_COLOR_RUNNING))
        self._run_tree.scrollToItem(item)
        self._highlighted_run_row = flat_index

    def mark_step_done(self, flat_index: int) -> None:
        """Mark the run-log row at flat_index as done (light green)."""
        if self._run_item(flat_index) is None:
            return
        self._paint_run_row(flat_index, QColor(_COLOR_DONE))
        if self._highlighted_run_row == flat_index:
            self._highlighted_run_row = None

    def _paint_run_row(self, flat_index: int, colour: QColor) -> None:
        item = self._run_items[flat_index]
        for column in range(self._run_tree.columnCount()):
            item.setBackground(column, QBrush(colour))

    def clear_highlights(self) -> None:
        """Reset every editor item to its base device colour."""
        for item, action in self._item_to_action.values():
            colour = (QColor(_COLORS["general"]) if isinstance(action, ForLoopAction)
                      else _base_color(action))
            for column in range(self._tree.columnCount()):
                item.setBackground(column, QBrush(colour))

    # ── Tree population ────────────────────────────────────────────────────

    def _populate(self, actions: list) -> None:
        for action in actions:
            if isinstance(action, ForLoopAction):
                self._add_loop_item(action)
            else:
                item = self._make_primitive_item(action, draggable=True)
                self._tree.addTopLevelItem(item)

    def _add_loop_item(self, action: ForLoopAction) -> QTreeWidgetItem:
        """Create a collapsible ForLoopAction group with body children."""
        loop_item = QTreeWidgetItem([action.describe()])
        for column in range(self._tree.columnCount()):
            loop_item.setBackground(column, QBrush(QColor(_COLORS["general"])))
        loop_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        self._set_action(loop_item, action)
        self._tree.addTopLevelItem(loop_item)
        loop_item.setExpanded(True)

        for body_action in action.body:
            loop_item.addChild(self._make_body_item(body_action))

        return loop_item

    def _make_body_item(self, action: Action) -> QTreeWidgetItem:
        """Create a loop-body child item — a normal leaf, or (for a nested
        ForLoopAction produced by the DSL, never by Visual itself) an opaque
        placeholder that can be moved/deleted as a block but not edited."""
        if isinstance(action, ForLoopAction):
            return self._make_nested_loop_placeholder_item(action)
        return self._make_primitive_item(action, draggable=False)

    def _make_nested_loop_placeholder_item(self, action: ForLoopAction) -> QTreeWidgetItem:
        item = QTreeWidgetItem([_nested_loop_label(action)])
        for column in range(self._tree.columnCount()):
            item.setBackground(column, QBrush(QColor(_COLORS["general"])))
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self._set_action(item, action)
        return item

    def _make_primitive_item(self, action: Action, *, draggable: bool) -> QTreeWidgetItem:
        item = QTreeWidgetItem([action.describe()])
        for column in range(self._tree.columnCount()):
            item.setBackground(column, QBrush(_base_color(action)))
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if draggable:
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        item.setFlags(flags)
        self._set_action(item, action)
        return item

    # ── ForLoopAction body reconstruction ───────────────────────────────────

    def _rebuild_loop_action(
        self, loop_item: QTreeWidgetItem, cached: ForLoopAction
    ) -> ForLoopAction:
        """Reconstruct a ForLoopAction from `loop_item`'s CURRENT tree
        children — the tree, not `cached.body`, is the source of truth for
        body contents once a loop is on screen (add/edit/delete/move-in-loop
        only ever touch the tree). `var`/`values` come from `cached`, which
        is kept current by ForLoopEditorDialog / _replace_top_level."""
        body: list[Action] = []
        for j in range(loop_item.childCount()):
            child_action = self._get_action(loop_item.child(j))
            if child_action is not None:
                body.append(child_action)
        return ForLoopAction(var=cached.var, values=cached.values, body=body)

    def _refresh_loop_header_text(self, loop_item: QTreeWidgetItem) -> None:
        action = self._get_action(loop_item)
        if isinstance(action, ForLoopAction):
            loop_item.setText(0, self._rebuild_loop_action(loop_item, action).describe())

    def _on_about_to_reorder(self) -> None:
        self._drag_snapshot = self._snapshot()

    def _on_rows_reordered(self) -> None:
        before, self._drag_snapshot = self._drag_snapshot, None
        if before is not None and not _same_actions(
                before[0], self.get_sequence().actions):
            self._push_undo(before)
        self.sequence_changed.emit()

    # ── Undo ───────────────────────────────────────────────────────────────

    def _can_edit(self) -> bool:
        return self._editing_allowed and self._stack.currentIndex() == 0

    def _item_path(self, item: QTreeWidgetItem | None) -> tuple[int, ...] | None:
        if item is None:
            return None
        parent = item.parent()
        if parent is None:
            return (self._tree.indexOfTopLevelItem(item),)
        return (self._tree.indexOfTopLevelItem(parent), parent.indexOfChild(item))

    def _item_at_path(self, path: tuple[int, ...] | None) -> QTreeWidgetItem | None:
        if not path:
            return None
        item = self._tree.topLevelItem(path[0])
        if item is not None and len(path) > 1:
            item = item.child(path[1])
        return item

    def _snapshot(self) -> tuple[list[Action], tuple[int, ...] | None]:
        return (copy.deepcopy(self.get_sequence().actions),
                self._item_path(self._tree.currentItem()))

    def _push_undo(self, state=None) -> None:
        """Record the state before an edit. Call just before changing the tree."""
        self._undo_stack.append(state if state is not None else self._snapshot())
        del self._undo_stack[:-_UNDO_LIMIT]
        self._sync_editing_controls()

    def _on_undo(self) -> None:
        if not self._undo_stack or not self._can_edit():
            return
        actions, path = self._undo_stack.pop()
        self._tree.clear()
        self._item_to_action.clear()
        self._populate(actions)
        item = self._item_at_path(path)
        if item is not None:
            self._select_items([item])
        self._sync_editing_controls()
        self.sequence_changed.emit()

    # ── Selection helpers ──────────────────────────────────────────────────

    def _current_selected(self) -> QTreeWidgetItem | None:
        """Whatever is currently selected — top-level item, loop header, or
        loop-body child (including a nested-loop placeholder)."""
        return self._tree.currentItem()

    def _selected_in_tree_order(self) -> list[QTreeWidgetItem]:
        """Selected editor items in display order, leaving out the children
        of a selected loop header (the header already stands for them)."""
        items: list[QTreeWidgetItem] = []
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            if top.isSelected():
                items.append(top)
                continue
            for j in range(top.childCount()):
                if top.child(j).isSelected():
                    items.append(top.child(j))
        return items

    def _select_items(self, items: list[QTreeWidgetItem]) -> None:
        """Select exactly `items` (last one current). setCurrentItem() alone
        would add to the old selection while Ctrl is still held (Ctrl+V/Z)."""
        self._tree.clearSelection()
        self._tree.setCurrentItem(
            items[-1], 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        for item in items:
            item.setSelected(True)

    def _current_top_level(self) -> QTreeWidgetItem | None:
        """Return the selected item only if it is a top-level item (not a body child)."""
        item = self._tree.currentItem()
        if item is None or item.parent() is not None:
            return None
        return item

    def _loop_body_insert_index(
        self, item: QTreeWidgetItem | None, *, after: bool
    ) -> tuple[QTreeWidgetItem, int] | None:
        """If `item` places an insert/paste inside a loop body, return
        (loop_header_item, body_index). Otherwise None (top-level context).

        A loop-header selection always targets the end of its own body
        (there's no "sibling" position to be above/below), regardless of
        `after`. A loop-body-child selection (plain or nested-loop
        placeholder) inserts before/after that child within the same body.
        """
        if item is None:
            return None
        action = self._get_action(item)
        if item.parent() is None:
            if isinstance(action, ForLoopAction):
                return item, item.childCount()
            return None
        parent = item.parent()
        idx = parent.indexOfChild(item)
        return parent, (idx + 1 if after else idx)

    def _update_context_label(self) -> None:
        ctx = self._loop_body_insert_index(self._current_selected(), after=True)
        if ctx is None:
            self._context_label.setVisible(False)
            return
        loop_item, _ = ctx
        loop_action = self._get_action(loop_item)
        var = loop_action.var if isinstance(loop_action, ForLoopAction) else "?"
        self._context_label.setText(f"Adding into loop '{var}'")
        self._context_label.setVisible(True)

    # ── Toolbar handlers ───────────────────────────────────────────────────

    def _on_add(self) -> None:
        try:
            from .step_editor import StepEditorDialog
        except ImportError:
            QMessageBox.information(
                self, "Add Step", "StepEditorDialog is coming in Task 7."
            )
            return

        ctx = self._loop_body_insert_index(self._current_selected(), after=True)
        if ctx is not None:
            loop_item, body_idx = ctx
            loop_action = self._get_action(loop_item)
            dlg = StepEditorDialog(parent=self, available_loop_vars=(loop_action.var,),
                                   spectrum_positions=self.spectrum_position_names(),
                                   global_positions=self.global_positions())
            if dlg.exec():
                action = dlg.get_action()
                if action is not None:
                    self._push_undo()
                    new_item = self._insert_body_action(loop_item, action, body_idx)
                    self._tree.setCurrentItem(new_item)
            return

        dlg = StepEditorDialog(parent=self, spectrum_positions=self.spectrum_position_names(),
                               global_positions=self.global_positions())
        if dlg.exec():
            action = dlg.get_action()
            if action is not None:
                self._push_undo()
                new_item = self._insert_action(action)
                self._tree.setCurrentItem(new_item)

    def _on_add_loop(self) -> None:
        try:
            from .for_loop_editor import ForLoopEditorDialog
        except ImportError:
            QMessageBox.information(
                self, "Add Loop", "ForLoopEditorDialog is not available."
            )
            return
        dlg = ForLoopEditorDialog(parent=self)
        if dlg.exec():
            action = dlg.get_action()
            if action is not None:
                # Always top-level: Phase 2 does not support nesting from Visual.
                self._push_undo()
                self._insert_action(action)

    def _on_edit(self) -> None:
        item = self._current_selected()
        if item is None:
            return
        action = self._get_action(item)
        if action is None:
            return

        if isinstance(action, ForLoopAction):
            if item.parent() is not None:
                QMessageBox.information(
                    self, "Nested Loop",
                    "This loop is nested inside another loop (created via the "
                    "Script tab). Nested loops can only be edited in the "
                    "Script tab.",
                )
                return
            try:
                from .for_loop_editor import ForLoopEditorDialog
            except ImportError:
                return
            live_action = self._rebuild_loop_action(item, action)
            dlg = ForLoopEditorDialog(action=live_action, parent=self)
            if dlg.exec():
                new_action = dlg.get_action()
                if new_action is not None:
                    self._push_undo()
                    self._replace_top_level(item, new_action)
            return

        try:
            from .step_editor import StepEditorDialog
        except ImportError:
            QMessageBox.information(
                self, "Edit Step", "StepEditorDialog is coming in Task 7."
            )
            return

        parent = item.parent()
        available_vars: tuple[str, ...] = ()
        if parent is not None:
            loop_action = self._get_action(parent)
            if isinstance(loop_action, ForLoopAction):
                available_vars = (loop_action.var,)

        dlg = StepEditorDialog(action=action, parent=self, available_loop_vars=available_vars,
                               spectrum_positions=self.spectrum_position_names(),
                               global_positions=self.global_positions())
        if dlg.exec():
            new_action = dlg.get_action()
            if new_action is not None:
                self._push_undo()
                if parent is not None:
                    self._replace_body_child(parent, item, new_action)
                else:
                    self._replace_top_level(item, new_action)

    def _on_delete(self) -> None:
        items = self._selected_in_tree_order()
        if not items:
            item = self._current_selected()
            items = [] if item is None else [item]
        if not items:
            return

        loop_steps = [
            item.childCount() for item in items
            if item.parent() is None
            and isinstance(self._get_action(item), ForLoopAction)
            and item.childCount() > 0
        ]
        if loop_steps:
            if len(loop_steps) == 1:
                text = (f"This loop contains {loop_steps[0]} step(s). "
                        "Delete the entire loop?")
            else:
                text = (f"The selection contains {len(loop_steps)} loops "
                        f"({sum(loop_steps)} step(s) in total). "
                        "Delete them entirely?")
            reply = QMessageBox.question(
                self, "Delete Loop", text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._push_undo()
        for item in items:
            parent = item.parent()
            if parent is not None:
                idx = parent.indexOfChild(item)
                if idx >= 0:
                    parent.takeChild(idx)
                    self._del_action(item)
                    self._refresh_loop_header_text(parent)
                continue
            idx = self._tree.indexOfTopLevelItem(item)
            if idx < 0:
                continue
            self._tree.takeTopLevelItem(idx)
            self._del_action(item)
            for j in range(item.childCount()):
                self._del_action(item.child(j))
        self.sequence_changed.emit()

    def _on_move_up(self) -> None:
        item = self._current_selected()
        if item is None:
            return
        parent = item.parent()
        if parent is not None:
            self._move_body_child(parent, item, -1)
            return
        idx = self._tree.indexOfTopLevelItem(item)
        if idx <= 0:
            return
        self._push_undo()
        self._tree.takeTopLevelItem(idx)
        self._tree.insertTopLevelItem(idx - 1, item)
        self._tree.setCurrentItem(item)
        self.sequence_changed.emit()

    def _on_move_down(self) -> None:
        item = self._current_selected()
        if item is None:
            return
        parent = item.parent()
        if parent is not None:
            self._move_body_child(parent, item, 1)
            return
        idx = self._tree.indexOfTopLevelItem(item)
        if idx >= self._tree.topLevelItemCount() - 1:
            return
        self._push_undo()
        self._tree.takeTopLevelItem(idx)
        self._tree.insertTopLevelItem(idx + 1, item)
        self._tree.setCurrentItem(item)
        self.sequence_changed.emit()

    def _on_copy(self) -> None:
        if self._stack.currentIndex() != 0:
            return
        items = self._selected_in_tree_order()
        if not items:
            item = self._current_selected()
            items = [] if item is None else [item]
        copied: list[Action] = []
        for item in items:
            action = self._get_action(item)
            if action is None:
                continue
            if item.parent() is None and isinstance(action, ForLoopAction):
                # The tree holds the loop's current body, not action.body.
                action = self._rebuild_loop_action(item, action)
            copied.append(copy.deepcopy(action))
        if copied:
            self._clipboard = copied

    def _on_paste_above(self) -> None:
        self._do_paste(after=False)

    def _on_paste_below(self) -> None:
        self._do_paste(after=True)

    def _do_paste(self, *, after: bool) -> None:
        if not self._clipboard or not self._can_edit():
            return
        selected = self._selected_in_tree_order()
        if selected:
            item = selected[-1] if after else selected[0]
        else:
            item = self._current_selected()
        has_loop = any(isinstance(a, ForLoopAction) for a in self._clipboard)
        pasted = copy.deepcopy(self._clipboard)

        # A loop pasted next to a loop header goes beside it at top level;
        # plain steps go into that loop's body (see _loop_body_insert_index).
        ctx = None
        if not (has_loop and item is not None and item.parent() is None):
            ctx = self._loop_body_insert_index(item, after=after)

        if ctx is not None:
            if has_loop:
                QMessageBox.warning(
                    self, "Paste",
                    "Loops cannot be pasted inside another loop "
                    "(nesting is not supported from the Visual editor).",
                )
                return
            loop_item, body_idx = ctx
            loop_action = self._get_action(loop_item)
            self._constantify_out_of_scope_vars(pasted, loop_action.var)
            self._push_undo()
            new_items = [
                self._insert_body_action(loop_item, action, body_idx + offset)
                for offset, action in enumerate(pasted)
            ]
        else:
            if item is None:
                idx = 0 if not after else self._tree.topLevelItemCount()
            else:
                top = item if item.parent() is None else item.parent()
                idx = self._tree.indexOfTopLevelItem(top) + (1 if after else 0)
            self._constantify_out_of_scope_vars(
                [a for a in pasted if not isinstance(a, ForLoopAction)], None)
            self._push_undo()
            new_items = [
                self._insert_action_at(action, idx + offset)
                for offset, action in enumerate(pasted)
            ]

        # Select the pasted block so a further Ctrl+V continues below it.
        self._select_items(new_items)

    def _constantify_out_of_scope_vars(
        self, actions: list[Action], in_scope_var: str | None
    ) -> None:
        """If an action directly references a loop variable other than
        `in_scope_var` (None = top level, no loop variable available), reset
        that field to 0.0 and warn once. Prevents Copy/Paste across a loop
        boundary from silently leaving a dangling variable reference."""
        refs: list[str] = []
        for action in actions:
            ref = action_loop_var_ref(action)
            if ref is not None and ref != in_scope_var:
                setattr(action, LOOP_VAR_FIELDS[type(action)], 0.0)
                if ref not in refs:
                    refs.append(ref)
        if refs:
            names = ", ".join(f"'{ref}'" for ref in refs)
            QMessageBox.warning(
                self, "Paste",
                f"The pasted step(s) referenced loop variable {names}, which "
                "is not available here. The value has been reset to 0.0 — "
                "please review it.",
            )

    # ── Insert / replace / delete / move helpers (top level) ───────────────

    def _insert_action(self, action: Action) -> QTreeWidgetItem:
        """Insert action after the selected top-level item, or at end."""
        selected = self._current_top_level()
        if selected is not None:
            insert_idx = self._tree.indexOfTopLevelItem(selected) + 1
        else:
            insert_idx = self._tree.topLevelItemCount()
        return self._insert_action_at(action, insert_idx)

    def _insert_action_at(self, action: Action, insert_idx: int) -> QTreeWidgetItem:
        """Insert action at the given top-level index."""
        if isinstance(action, ForLoopAction):
            item = self._add_loop_item(action)   # appended at end
            last = self._tree.topLevelItemCount() - 1
            if insert_idx <= last:
                moved = self._tree.takeTopLevelItem(last)
                self._tree.insertTopLevelItem(insert_idx, moved)
        else:
            item = self._make_primitive_item(action, draggable=True)
            self._tree.insertTopLevelItem(insert_idx, item)

        self.sequence_changed.emit()
        return item

    def _replace_top_level(
        self, old_item: QTreeWidgetItem, new_action: Action
    ) -> None:
        """Replace old_item with a new action, keeping its position."""
        idx = self._tree.indexOfTopLevelItem(old_item)
        if idx < 0:
            return

        # Remove old
        self._tree.takeTopLevelItem(idx)
        self._del_action(old_item)
        for j in range(old_item.childCount()):
            self._del_action(old_item.child(j))

        # Insert new at same position
        if isinstance(new_action, ForLoopAction):
            self._add_loop_item(new_action)  # appended at end
            last = self._tree.topLevelItemCount() - 1
            if idx <= last:
                moved = self._tree.takeTopLevelItem(last)
                self._tree.insertTopLevelItem(idx, moved)
        else:
            item = self._make_primitive_item(new_action, draggable=True)
            self._tree.insertTopLevelItem(idx, item)

        self.sequence_changed.emit()

    # ── Insert / replace / delete / move helpers (loop body) ───────────────

    def _insert_body_action(
        self, loop_item: QTreeWidgetItem, action: Action, idx: int
    ) -> QTreeWidgetItem:
        child = self._make_body_item(action)
        loop_item.insertChild(idx, child)
        self._refresh_loop_header_text(loop_item)
        self.sequence_changed.emit()
        return child

    def _replace_body_child(
        self, loop_item: QTreeWidgetItem, old_child: QTreeWidgetItem, new_action: Action
    ) -> None:
        idx = loop_item.indexOfChild(old_child)
        if idx < 0:
            return
        loop_item.takeChild(idx)
        self._del_action(old_child)
        loop_item.insertChild(idx, self._make_body_item(new_action))
        self.sequence_changed.emit()

    def _delete_body_child(self, loop_item: QTreeWidgetItem, child: QTreeWidgetItem) -> None:
        idx = loop_item.indexOfChild(child)
        if idx < 0:
            return
        loop_item.takeChild(idx)
        self._del_action(child)
        self._refresh_loop_header_text(loop_item)
        self.sequence_changed.emit()

    def _move_body_child(self, loop_item: QTreeWidgetItem, child: QTreeWidgetItem, delta: int) -> None:
        idx = loop_item.indexOfChild(child)
        new_idx = idx + delta
        if idx < 0 or new_idx < 0 or new_idx >= loop_item.childCount():
            return
        self._push_undo()
        loop_item.takeChild(idx)
        loop_item.insertChild(new_idx, child)
        self._tree.setCurrentItem(child)
        self.sequence_changed.emit()

    # ── _item_to_action helpers ────────────────────────────────────────────

    def _get_action(self, item: QTreeWidgetItem | None) -> Action | None:
        if item is None:
            return None
        entry = self._item_to_action.get(id(item))
        return entry[1] if entry is not None else None

    def _set_action(self, item: QTreeWidgetItem, action: Action) -> None:
        self._item_to_action[id(item)] = (item, action)

    def _del_action(self, item: QTreeWidgetItem | None) -> None:
        if item is not None:
            self._item_to_action.pop(id(item), None)

    # ── Utilities ──────────────────────────────────────────────────────────

    def _on_app_focus_changed(self, old_widget, new_widget) -> None:
        """Clear tree selection when focus moves outside this TimelineWidget."""
        if new_widget is None:
            return
        w = new_widget
        while w is not None:
            if w is self:
                return
            w = w.parent()
        self._tree.clearSelection()
        self._tree.setCurrentItem(None)

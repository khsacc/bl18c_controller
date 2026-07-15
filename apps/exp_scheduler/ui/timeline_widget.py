"""
TimelineWidget — visual sequence timeline for the Experimental Scheduler.

flat_index semantics mirror SequenceRunner._flat_index:
  - ForLoopAction itself is NOT counted.
  - Each primitive action execution (including body steps on every iteration)
    gets a sequential flat_index.

Example — sequence: [action_a, for p in [0.5, 1.0]: [action_b, action_c], action_d]
  flat 0 → action_a
  flat 1 → action_b  (iter 0)
  flat 2 → action_c  (iter 0)
  flat 3 → action_b  (iter 1)   ← same tree item as flat 1
  flat 4 → action_c  (iter 1)
  flat 5 → action_d

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
"""
from __future__ import annotations

import copy

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..actions import (
    Action,
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
    StageAction,
    StartFollowingAction,
    StopFollowingAction,
    TakeDarkAction,
    TakeXrdAction,
    WaitPressureAction,
    WaitTemperatureAction,
    action_loop_var_ref,
)
from ..sequence import Sequence


# ── Color palette ──────────────────────────────────────────────────────────────

_COLORS: dict[str, str] = {
    "stage":    "#e3f0ff",   # light blue
    "pace5000": "#fff3e0",   # light orange
    "lakeshore":"#fce4ec",   # light pink
    "xrd":      "#e8f5e9",   # light green
    "camera":   "#f3e5f5",   # light purple
    "general":  "#f5f5f5",   # light gray
}

_COLOR_RUNNING = "#fff176"   # yellow  — currently executing
_COLOR_DONE    = "#c8e6c9"   # light green — completed


def _device_key(action: Action) -> str:
    if isinstance(action, (StageAction, MicroscopeOutFpdInAction, FpdOutMicroscopeInAction)):
        return "stage"
    if isinstance(action, (SetPressureAction, WaitPressureAction, SetControlModeAction)):
        return "pace5000"
    if isinstance(action, (SetTemperatureAction, WaitTemperatureAction,
                           SetHeaterAction, AllHeatersOffAction)):
        return "lakeshore"
    if isinstance(action, (TakeDarkAction, TakeXrdAction)):
        return "xrd"
    if isinstance(action, (SaveReferenceImageAction, SaveSnapshotAction, StartFollowingAction,
                           StopFollowingAction, FollowSampleAction)):
        return "camera"
    return "general"


def _base_color(action: Action) -> QColor:
    return QColor(_COLORS[_device_key(action)])


def _nested_loop_label(action: ForLoopAction) -> str:
    return f"⚠ Nested loop — edit via Script tab: {action.describe()}"


# ── QTreeWidget subclass ───────────────────────────────────────────────────────

class _SequenceTree(QTreeWidget):
    """
    QTreeWidget that restricts InternalMove drops to top-level reordering.
    Dropping an item OnItem (which would make it a child) is blocked.
    """

    rows_reordered = pyqtSignal()

    def dropEvent(self, event) -> None:
        if (self.dropIndicatorPosition()
                == QAbstractItemView.DropIndicatorPosition.OnItem):
            event.ignore()
            return
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

        # id(item) → (item, Action)  — QTreeWidgetItem is not hashable in PyQt6
        self._item_to_action: dict[int, tuple[QTreeWidgetItem, Action]] = {}
        self._clipboard: Action | None = None

        # flat_map[i] = (tree_item, iteration_index | None, loop_group_item | None)
        # Matches SequenceRunner._flat_index exactly.
        self._flat_map: list[tuple[
            QTreeWidgetItem,
            int | None,
            QTreeWidgetItem | None,
        ]] = []

        self._highlighted_item: QTreeWidgetItem | None = None

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
        root.addWidget(self._make_tree())

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
        self._btn_copy.clicked.connect(self._on_copy)
        bar.addWidget(self._btn_copy)

        self._btn_paste_above = QPushButton("Paste ↑")
        self._btn_paste_above.clicked.connect(self._on_paste_above)
        bar.addWidget(self._btn_paste_above)

        self._btn_paste_below = QPushButton("Paste ↓")
        self._btn_paste_below.clicked.connect(self._on_paste_below)
        bar.addWidget(self._btn_paste_below)

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

        return bar

    def _make_tree(self) -> _SequenceTree:
        self._tree = _SequenceTree(self)
        self._tree.setColumnCount(1)
        self._tree.setHeaderHidden(True)
        self._tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.rows_reordered.connect(self._on_rows_reordered)
        self._tree.itemSelectionChanged.connect(self._update_context_label)
        return self._tree

    # ── Public API ─────────────────────────────────────────────────────────

    def set_sequence(self, sequence: Sequence) -> None:
        """Replace the displayed sequence. Clears all highlights."""
        self._tree.clear()
        self._item_to_action.clear()
        self._flat_map.clear()
        self._highlighted_item = None
        self._populate(sequence.actions)
        self._rebuild_flat_map()

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
        """
        Highlight the item at flat_index (yellow).
        Resets the previous highlighted item to its base device color.
        """
        # Restore previous highlighted item to base color
        if self._highlighted_item is not None:
            prev_action = self._get_action(self._highlighted_item)
            if prev_action is not None:
                self._highlighted_item.setBackground(
                    0, QBrush(_base_color(prev_action))
                )
        self._highlighted_item = None

        if flat_index >= len(self._flat_map):
            return

        item, iteration, loop_item = self._flat_map[flat_index]
        item.setBackground(0, QBrush(QColor(_COLOR_RUNNING)))
        self._tree.scrollToItem(item)
        self._highlighted_item = item

        # Update ForLoopAction group header with current iteration counter
        if loop_item is not None and iteration is not None:
            loop_action = self._get_action(loop_item)
            if isinstance(loop_action, ForLoopAction):
                n = len(loop_action.values)
                live = self._rebuild_loop_action(loop_item, loop_action)
                loop_item.setText(0, f"{live.describe()}  [iter {iteration + 1}/{n}]")

    def mark_step_done(self, flat_index: int) -> None:
        """Mark the item at flat_index as done (light green)."""
        if flat_index >= len(self._flat_map):
            return
        item, _, _ = self._flat_map[flat_index]
        item.setBackground(0, QBrush(QColor(_COLOR_DONE)))
        if self._highlighted_item is item:
            self._highlighted_item = None

    def clear_highlights(self) -> None:
        """Reset every item to its base device color and restore loop headers
        / nested-loop placeholder labels."""
        self._highlighted_item = None
        for item, action in self._item_to_action.values():
            if isinstance(action, ForLoopAction):
                item.setBackground(0, QBrush(QColor(_COLORS["general"])))
                if item.parent() is None:
                    item.setText(0, self._rebuild_loop_action(item, action).describe())
                else:
                    item.setText(0, _nested_loop_label(action))
            else:
                item.setBackground(0, QBrush(_base_color(action)))

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
        loop_item.setBackground(0, QBrush(QColor(_COLORS["general"])))
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
        item.setBackground(0, QBrush(QColor(_COLORS["general"])))
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self._set_action(item, action)
        return item

    def _make_primitive_item(self, action: Action, *, draggable: bool) -> QTreeWidgetItem:
        item = QTreeWidgetItem([action.describe()])
        item.setBackground(0, QBrush(_base_color(action)))
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

    # ── Flat-map rebuild ───────────────────────────────────────────────────

    def _rebuild_flat_map(self) -> None:
        """
        Rebuild flat_map by scanning the current tree state.
        Must be called after any structural change (add / delete / reorder).
        """
        self._flat_map.clear()
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            action = self._get_action(top)
            if action is None:
                continue
            if isinstance(action, ForLoopAction):
                body_items = [top.child(j) for j in range(top.childCount())]
                for iteration in range(len(action.values)):
                    for child in body_items:
                        self._flat_map.append((child, iteration, top))
            else:
                self._flat_map.append((top, None, None))

    def _on_rows_reordered(self) -> None:
        self._rebuild_flat_map()
        self.sequence_changed.emit()

    # ── Selection helpers ──────────────────────────────────────────────────

    def _current_selected(self) -> QTreeWidgetItem | None:
        """Whatever is currently selected — top-level item, loop header, or
        loop-body child (including a nested-loop placeholder)."""
        return self._tree.currentItem()

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
            dlg = StepEditorDialog(parent=self, available_loop_vars=(loop_action.var,))
            if dlg.exec():
                action = dlg.get_action()
                if action is not None:
                    self._insert_body_action(loop_item, action, body_idx)
            return

        dlg = StepEditorDialog(parent=self)
        if dlg.exec():
            action = dlg.get_action()
            if action is not None:
                self._insert_action(action)

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

        dlg = StepEditorDialog(action=action, parent=self, available_loop_vars=available_vars)
        if dlg.exec():
            new_action = dlg.get_action()
            if new_action is not None:
                if parent is not None:
                    self._replace_body_child(parent, item, new_action)
                else:
                    self._replace_top_level(item, new_action)

    def _on_delete(self) -> None:
        item = self._current_selected()
        if item is None:
            return
        action = self._get_action(item)
        parent = item.parent()

        if parent is None and isinstance(action, ForLoopAction):
            n = item.childCount()
            if n > 0:
                reply = QMessageBox.question(
                    self, "Delete Loop",
                    f"This loop contains {n} step(s). Delete the entire loop?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
            idx = self._tree.indexOfTopLevelItem(item)
            if idx < 0:
                return
            self._tree.takeTopLevelItem(idx)
            self._del_action(item)
            for j in range(item.childCount()):
                self._del_action(item.child(j))
            self._rebuild_flat_map()
            self.sequence_changed.emit()
            return

        if parent is not None:
            self._delete_body_child(parent, item)
            return

        idx = self._tree.indexOfTopLevelItem(item)
        if idx < 0:
            return
        self._tree.takeTopLevelItem(idx)
        self._del_action(item)
        self._rebuild_flat_map()
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
        self._tree.takeTopLevelItem(idx)
        self._tree.insertTopLevelItem(idx - 1, item)
        self._tree.setCurrentItem(item)
        self._rebuild_flat_map()
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
        self._tree.takeTopLevelItem(idx)
        self._tree.insertTopLevelItem(idx + 1, item)
        self._tree.setCurrentItem(item)
        self._rebuild_flat_map()
        self.sequence_changed.emit()

    def _on_copy(self) -> None:
        item = self._current_selected()
        if item is None:
            return
        action = self._get_action(item)
        if action is not None:
            self._clipboard = copy.deepcopy(action)

    def _on_paste_above(self) -> None:
        self._do_paste(after=False)

    def _on_paste_below(self) -> None:
        self._do_paste(after=True)

    def _do_paste(self, *, after: bool) -> None:
        if self._clipboard is None:
            return
        item = self._current_selected()

        ctx = self._loop_body_insert_index(item, after=after)
        if ctx is not None:
            loop_item, body_idx = ctx
            if isinstance(self._clipboard, ForLoopAction):
                QMessageBox.warning(
                    self, "Paste",
                    "Loops cannot be pasted inside another loop "
                    "(nesting is not supported from the Visual editor).",
                )
                return
            loop_action = self._get_action(loop_item)
            pasted = copy.deepcopy(self._clipboard)
            self._constantify_out_of_scope_var(pasted, loop_action.var)
            self._insert_body_action(loop_item, pasted, body_idx)
            return

        if item is None:
            idx = 0 if not after else self._tree.topLevelItemCount()
        else:
            idx = self._tree.indexOfTopLevelItem(item) + (1 if after else 0)
        pasted = copy.deepcopy(self._clipboard)
        if not isinstance(pasted, ForLoopAction):
            self._constantify_out_of_scope_var(pasted, None)
        self._insert_action_at(pasted, idx)

    def _constantify_out_of_scope_var(self, action: Action, in_scope_var: str | None) -> None:
        """If `action` directly references a loop variable other than
        `in_scope_var` (None = top level, no loop variable available), reset
        that field to 0.0 and warn. Prevents Copy/Paste across a loop
        boundary from silently leaving a dangling variable reference."""
        ref = action_loop_var_ref(action)
        if ref is not None and ref != in_scope_var:
            field = LOOP_VAR_FIELDS[type(action)]
            setattr(action, field, 0.0)
            QMessageBox.warning(
                self, "Paste",
                f"The pasted step referenced loop variable '{ref}', which is "
                "not available here. Its value has been reset to 0.0 — "
                "please review it.",
            )

    # ── Insert / replace / delete / move helpers (top level) ───────────────

    def _insert_action(self, action: Action) -> None:
        """Insert action after the selected top-level item, or at end."""
        selected = self._current_top_level()
        if selected is not None:
            insert_idx = self._tree.indexOfTopLevelItem(selected) + 1
        else:
            insert_idx = self._tree.topLevelItemCount()
        self._insert_action_at(action, insert_idx)

    def _insert_action_at(self, action: Action, insert_idx: int) -> None:
        """Insert action at the given top-level index."""
        if isinstance(action, ForLoopAction):
            self._add_loop_item(action)   # appended at end
            last = self._tree.topLevelItemCount() - 1
            if insert_idx <= last:
                moved = self._tree.takeTopLevelItem(last)
                self._tree.insertTopLevelItem(insert_idx, moved)
        else:
            item = self._make_primitive_item(action, draggable=True)
            self._tree.insertTopLevelItem(insert_idx, item)

        self._rebuild_flat_map()
        self.sequence_changed.emit()

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

        self._rebuild_flat_map()
        self.sequence_changed.emit()

    # ── Insert / replace / delete / move helpers (loop body) ───────────────

    def _insert_body_action(self, loop_item: QTreeWidgetItem, action: Action, idx: int) -> None:
        child = self._make_body_item(action)
        loop_item.insertChild(idx, child)
        self._refresh_loop_header_text(loop_item)
        self._rebuild_flat_map()
        self.sequence_changed.emit()

    def _replace_body_child(
        self, loop_item: QTreeWidgetItem, old_child: QTreeWidgetItem, new_action: Action
    ) -> None:
        idx = loop_item.indexOfChild(old_child)
        if idx < 0:
            return
        loop_item.takeChild(idx)
        self._del_action(old_child)
        loop_item.insertChild(idx, self._make_body_item(new_action))
        self._rebuild_flat_map()
        self.sequence_changed.emit()

    def _delete_body_child(self, loop_item: QTreeWidgetItem, child: QTreeWidgetItem) -> None:
        idx = loop_item.indexOfChild(child)
        if idx < 0:
            return
        loop_item.takeChild(idx)
        self._del_action(child)
        self._refresh_loop_header_text(loop_item)
        self._rebuild_flat_map()
        self.sequence_changed.emit()

    def _move_body_child(self, loop_item: QTreeWidgetItem, child: QTreeWidgetItem, delta: int) -> None:
        idx = loop_item.indexOfChild(child)
        new_idx = idx + delta
        if idx < 0 or new_idx < 0 or new_idx >= loop_item.childCount():
            return
        loop_item.takeChild(idx)
        loop_item.insertChild(new_idx, child)
        self._tree.setCurrentItem(child)
        self._rebuild_flat_map()
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

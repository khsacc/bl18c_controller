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
"""
from __future__ import annotations

import copy

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
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
    MicroscopeOutFpdInAction,
    SaveReferenceImageAction,
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
    if isinstance(action, (SaveReferenceImageAction, StartFollowingAction,
                           StopFollowingAction, FollowSampleAction)):
        return "camera"
    return "general"


def _base_color(action: Action) -> QColor:
    return QColor(_COLORS[_device_key(action)])


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
                           (add / delete / reorder).  Consumers should call
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
        return bar

    def _make_tree(self) -> _SequenceTree:
        self._tree = _SequenceTree(self)
        self._tree.setColumnCount(1)
        self._tree.setHeaderHidden(True)
        self._tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.rows_reordered.connect(self._on_rows_reordered)
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
        """Return a Sequence that reflects the current tree order."""
        actions: list[Action] = []
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            action = self._get_action(item)
            if action is not None:
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
                loop_item.setText(
                    0,
                    f"{loop_action.describe()}  [iter {iteration + 1}/{n}]",
                )

    def mark_step_done(self, flat_index: int) -> None:
        """Mark the item at flat_index as done (light green)."""
        if flat_index >= len(self._flat_map):
            return
        item, _, _ = self._flat_map[flat_index]
        item.setBackground(0, QBrush(QColor(_COLOR_DONE)))
        if self._highlighted_item is item:
            self._highlighted_item = None

    def clear_highlights(self) -> None:
        """Reset every item to its base device color and restore loop headers."""
        self._highlighted_item = None
        for item, action in self._item_to_action.values():
            if isinstance(action, ForLoopAction):
                item.setBackground(0, QBrush(QColor(_COLORS["general"])))
                item.setText(0, action.describe())
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
            child = self._make_primitive_item(body_action, draggable=False)
            loop_item.addChild(child)

        return loop_item

    def _make_primitive_item(self, action: Action, *, draggable: bool) -> QTreeWidgetItem:
        item = QTreeWidgetItem([action.describe()])
        item.setBackground(0, QBrush(_base_color(action)))
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if draggable:
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        item.setFlags(flags)
        self._set_action(item, action)
        return item

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

    # ── Toolbar handlers ───────────────────────────────────────────────────

    def _on_add(self) -> None:
        try:
            from .step_editor import StepEditorDialog
        except ImportError:
            QMessageBox.information(
                self, "Add Step", "StepEditorDialog is coming in Task 7."
            )
            return
        dlg = StepEditorDialog(parent=self)
        if dlg.exec():
            action = dlg.get_action()
            if action is not None:
                self._insert_action(action)

    def _on_edit(self) -> None:
        item = self._current_top_level()
        if item is None:
            return
        try:
            from .step_editor import StepEditorDialog
        except ImportError:
            QMessageBox.information(
                self, "Edit Step", "StepEditorDialog is coming in Task 7."
            )
            return
        action = self._get_action(item)
        if action is None:
            return
        dlg = StepEditorDialog(action=action, parent=self)
        if dlg.exec():
            new_action = dlg.get_action()
            if new_action is not None:
                self._replace_top_level(item, new_action)

    def _on_delete(self) -> None:
        item = self._current_top_level()
        if item is None:
            return
        idx = self._tree.indexOfTopLevelItem(item)
        if idx < 0:
            return
        self._tree.takeTopLevelItem(idx)
        # Clean up all entries related to this item and its children
        self._del_action(item)
        for j in range(item.childCount()):
            self._del_action(item.child(j))
        self._rebuild_flat_map()
        self.sequence_changed.emit()

    def _on_move_up(self) -> None:
        item = self._current_top_level()
        if item is None:
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
        item = self._current_top_level()
        if item is None:
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
        item = self._current_top_level()
        if item is None:
            return
        action = self._get_action(item)
        if action is not None:
            self._clipboard = copy.deepcopy(action)

    def _on_paste_above(self) -> None:
        if self._clipboard is None:
            return
        item = self._current_top_level()
        if item is None:
            idx = 0
        else:
            idx = self._tree.indexOfTopLevelItem(item)
        self._insert_action_at(copy.deepcopy(self._clipboard), idx)

    def _on_paste_below(self) -> None:
        if self._clipboard is None:
            return
        item = self._current_top_level()
        if item is None:
            idx = self._tree.topLevelItemCount()
        else:
            idx = self._tree.indexOfTopLevelItem(item) + 1
        self._insert_action_at(copy.deepcopy(self._clipboard), idx)

    # ── Insert / replace helpers ───────────────────────────────────────────

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

    def _current_top_level(self) -> QTreeWidgetItem | None:
        """Return the selected item only if it is a top-level item (not a body child)."""
        item = self._tree.currentItem()
        if item is None or item.parent() is not None:
            return None
        return item

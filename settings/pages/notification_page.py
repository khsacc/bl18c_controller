"""Settings page — completion sound notification."""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox, QGroupBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

try:
    from settings import notification_prefs
    from settings.notification_sound import SOUND_OPTIONS, play_done_sound
    from settings.i18n import tr
except ImportError:
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from settings import notification_prefs
    from settings.notification_sound import SOUND_OPTIONS, play_done_sound
    from settings.i18n import tr


class NotificationPage(QWidget):
    """Settings page for sound notification on measurement completion."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()
        self._refresh()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        sound_group = QGroupBox(
            tr("Notification Sound (Completion of Collimator scan, DAC scan, and XRD measurement)")
        )
        sound_lay = QVBoxLayout(sound_group)
        sound_lay.setSpacing(10)

        sound_lay.addWidget(QLabel(
            tr(
                "Select a sound to notify the user when the operation is completed.\n"
                "Source: OtoLogic (https://otologic.jp/), 効果音ラボ (https://soundeffect-lab.info/)"
            )
        ))

        row = QHBoxLayout()
        self._combo = QComboBox()
        for i, (key, label, _) in enumerate(SOUND_OPTIONS):
            self._combo.addItem(label)
            self._combo.setItemData(i, key)
        self._combo.currentIndexChanged.connect(self._on_changed)
        row.addWidget(self._combo)
        row.addStretch()
        sound_lay.addLayout(row)

        root.addWidget(sound_group)
        root.addStretch()

    def _refresh(self) -> None:
        key = notification_prefs.get_sound()
        idx = next((i for i, (k, *_) in enumerate(SOUND_OPTIONS) if k == key), 1)
        self._combo.blockSignals(True)
        self._combo.setCurrentIndex(idx)
        self._combo.blockSignals(False)

    def _on_changed(self, _index: int) -> None:
        key = self._combo.currentData()
        notification_prefs.set_sound(key)
        wav_name = next((w for k, _, w in SOUND_OPTIONS if k == key), None)
        play_done_sound(wav_name)

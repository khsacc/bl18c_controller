"""Settings page — Slack notifications for the Experimental Scheduler."""
from __future__ import annotations

import threading

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from settings import slack_prefs
from settings.slack_prefs import SlackPrefs


def _no_wheel(widget) -> None:
    widget.wheelEvent = lambda event: event.ignore()


class SlackPage(QWidget):
    # Results of Slack calls made on worker threads, delivered to the GUI thread.
    _channels_loaded = pyqtSignal(object, str)   # (list[dict] | None, error)
    _test_finished = pyqtSignal(str)            # error ("" = success)
    _check_finished = pyqtSignal(object)        # list[(ok, message)]

    def __init__(self, slack_service=None, parent=None) -> None:
        super().__init__(parent)
        self._service = slack_service
        self._channels_loaded.connect(self._on_channels_loaded)
        self._test_finished.connect(self._on_test_finished)
        self._check_finished.connect(self._on_check_finished)
        self._setup_ui()
        self._load(slack_prefs.load())
        self._refresh_status()
        if slack_service is not None:
            # The on/off switch can also be flipped from the scheduler window.
            listener = self._on_service_changed
            slack_service.add_listener(listener)
            self.destroyed.connect(lambda *_: slack_service.remove_listener(listener))

    def _on_service_changed(self) -> None:
        self._enabled.setChecked(self._service.prefs.enabled)
        self._refresh_status()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        intro = QLabel(
            "Posts a message to a Slack channel when an Experimental Scheduler sequence "
            "completes, ends on its own stop condition, or stops with an error. Nothing is "
            "posted when a sequence starts or when you press Stop."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        creds = QGroupBox("App credentials")
        creds_form = QFormLayout(creds)
        self._bot_token = QLineEdit()
        self._bot_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._bot_token.setPlaceholderText("xoxb-…  (OAuth & Permissions → Bot User OAuth Token)")
        self._app_token = QLineEdit()
        self._app_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._app_token.setPlaceholderText("xapp-…  (Basic Information → App-Level Tokens; "
                                           "slash commands only)")
        creds_form.addRow("Bot User OAuth Token:", self._bot_token)
        creds_form.addRow("App-Level Token:", self._app_token)
        creds_buttons = QHBoxLayout()
        self._show_tokens = QCheckBox("Show tokens")
        self._show_tokens.toggled.connect(self._on_show_tokens)
        creds_buttons.addWidget(self._show_tokens)
        creds_buttons.addStretch()
        self._btn_check = QPushButton("Check credentials")
        self._btn_check.setToolTip("Checks the tokens typed above against Slack, "
                                   "including the bot token scopes. Nothing is saved.")
        self._btn_check.clicked.connect(self._check_credentials)
        creds_buttons.addWidget(self._btn_check)
        self._btn_save_creds = QPushButton("Save credentials")
        self._btn_save_creds.clicked.connect(self._save_credentials)
        creds_buttons.addWidget(self._btn_save_creds)
        creds_form.addRow(creds_buttons)
        self._token_source = QLabel()
        self._token_source.setWordWrap(True)
        self._token_source.setStyleSheet("color: #555;")
        creds_form.addRow(self._token_source)
        root.addWidget(creds)

        notify = QGroupBox("Notifications")
        form = QFormLayout(notify)
        self._enabled = QCheckBox("Send notifications to Slack")
        form.addRow(self._enabled)

        channel_row = QHBoxLayout()
        self._channel = QComboBox()
        self._channel.setMinimumWidth(260)
        _no_wheel(self._channel)
        channel_row.addWidget(self._channel, 1)
        self._btn_refresh = QPushButton("Refresh channel list")
        self._btn_refresh.clicked.connect(self._refresh_channels)
        channel_row.addWidget(self._btn_refresh)
        self._btn_test = QPushButton("Send test message")
        self._btn_test.clicked.connect(self._send_test)
        channel_row.addWidget(self._btn_test)
        form.addRow("Channel:", channel_row)

        self._on_completion = QCheckBox("Sequence completed")
        self._on_condition = QCheckBox("Sequence ended by its stop condition (e.g. ruby pressure reached)")
        self._on_error = QCheckBox("Sequence stopped by an error")
        form.addRow("Notify when:", self._on_completion)
        form.addRow("", self._on_condition)
        form.addRow("", self._on_error)

        self._suppress = QSpinBox()
        self._suppress.setRange(0, 240)
        self._suppress.setSuffix(" min")
        self._suppress.setToolTip("Repeats of the same error within this time are counted and "
                                  "sent as one summary. 0 = never suppress.")
        _no_wheel(self._suppress)
        form.addRow("Group identical errors for:", self._suppress)
        root.addWidget(notify)

        slash = QGroupBox("Slash commands (direct message with the bot)")
        slash_form = QFormLayout(slash)
        self._slash_enabled = QCheckBox("Answer status / progress / steps slash commands")
        slash_form.addRow(self._slash_enabled)
        self._allowed = QLineEdit()
        self._allowed.setPlaceholderText("U01ABCDE2FG, U02HIJKL3MN")
        self._allowed.setToolTip("Slack member IDs (Profile → ⋮ → Copy member ID). "
                                 "Only these users get an answer.")
        slash_form.addRow("Allowed member IDs:", self._allowed)
        root.addWidget(slash)

        save_row = QHBoxLayout()
        save_row.addStretch()
        self._btn_save = QPushButton("Save Slack settings")
        self._btn_save.clicked.connect(self._save)
        save_row.addWidget(self._btn_save)
        root.addLayout(save_row)

        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #555;")
        root.addWidget(self._status)
        root.addStretch()

        if self._service is None:
            for widget in (self._btn_refresh, self._btn_test, self._btn_check):
                widget.setEnabled(False)

    def _load(self, prefs: SlackPrefs) -> None:
        bot_token, app_token = slack_prefs.load_credentials()
        self._bot_token.setText(bot_token)
        self._app_token.setText(app_token)
        self._enabled.setChecked(prefs.enabled)
        self._channel.clear()
        if prefs.channel_id:
            self._channel.addItem(f"#{prefs.channel_name or prefs.channel_id}",
                                  (prefs.channel_id, prefs.channel_name))
        else:
            self._channel.addItem("(press Refresh channel list)", None)
        self._on_completion.setChecked(prefs.notify_on_completion)
        self._on_condition.setChecked(prefs.notify_on_condition_stop)
        self._on_error.setChecked(prefs.notify_on_error)
        self._suppress.setValue(prefs.error_suppress_minutes)
        self._slash_enabled.setChecked(prefs.slash_commands_enabled)
        self._allowed.setText(", ".join(prefs.allowed_user_ids))

    def _refresh_status(self) -> None:
        if self._service is None:
            self._status.setText("Slack service is not running in this window.")
            self._token_source.setText("")
        else:
            self._status.setText(self._service.status_text())
            self._token_source.setText(
                f"In use — bot token: {self._service.token_source('bot')}; "
                f"app-level token: {self._service.token_source('app')}. "
                "An empty field falls back to the environment variable "
                "SLACK_BOT_TOKEN / SLACK_APP_TOKEN. Tokens are stored only on this PC."
            )

    # ── credentials ───────────────────────────────────────────────────────────

    def _on_show_tokens(self, show: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        self._bot_token.setEchoMode(mode)
        self._app_token.setEchoMode(mode)

    def _typed_tokens(self) -> tuple[str, str] | None:
        try:
            return slack_prefs.validate_tokens(self._bot_token.text(), self._app_token.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Slack", str(exc))
            return None

    def _save_credentials(self) -> None:
        tokens = self._typed_tokens()
        if tokens is None:
            return
        try:
            slack_prefs.save_credentials(*tokens)
        except OSError as exc:
            QMessageBox.warning(self, "Slack", f"Could not save the credentials:\n{exc}")
            return
        self._bot_token.setText(tokens[0])
        self._app_token.setText(tokens[1])
        if self._service is not None:
            self._service.update_credentials(*tokens)
        self._refresh_status()
        QMessageBox.information(self, "Slack", "Credentials saved and applied.")

    def _check_credentials(self) -> None:
        tokens = self._typed_tokens()
        if tokens is None:
            return
        # Empty fields are checked with the fallback the service would use.
        bot_token = tokens[0] or self._service.bot_token()
        app_token = tokens[1] or self._service.app_token()
        self._btn_check.setEnabled(False)
        self._token_source.setText("Checking with Slack…")
        service = self._service

        def work() -> None:
            try:
                self._check_finished.emit(service.check_credentials(bot_token, app_token))
            except Exception as exc:
                self._check_finished.emit([(False, str(exc))])

        threading.Thread(target=work, name="SlackCredentialCheck", daemon=True).start()

    def _on_check_finished(self, results) -> None:
        self._btn_check.setEnabled(True)
        self._refresh_status()
        text = "\n".join(("✔ " if ok else "✘ ") + message for ok, message in results)
        if all(ok for ok, _ in results):
            QMessageBox.information(self, "Slack credentials", text)
        else:
            QMessageBox.warning(self, "Slack credentials", text)

    # ── actions ───────────────────────────────────────────────────────────────

    def _selected_channel(self) -> tuple[str, str] | None:
        return self._channel.currentData()

    def _refresh_channels(self) -> None:
        self._btn_refresh.setEnabled(False)
        self._status.setText("Loading channel list…")
        service = self._service

        def work() -> None:
            try:
                self._channels_loaded.emit(service.list_public_channels(), "")
            except Exception as exc:
                self._channels_loaded.emit(None, str(exc))

        threading.Thread(target=work, name="SlackChannelList", daemon=True).start()

    def _on_channels_loaded(self, channels, error: str) -> None:
        self._btn_refresh.setEnabled(True)
        if channels is None:
            self._refresh_status()
            QMessageBox.warning(self, "Slack", f"Could not load the channel list:\n{error}")
            return
        previous = self._selected_channel()
        self._channel.clear()
        for ch in channels:
            self._channel.addItem(f"#{ch['name']}", (ch["id"], ch["name"]))
        if previous is not None:
            index = self._channel.findData(previous)
            if index < 0:
                for i in range(self._channel.count()):
                    if self._channel.itemData(i)[0] == previous[0]:
                        index = i
                        break
            if index >= 0:
                self._channel.setCurrentIndex(index)
        self._refresh_status()
        if not channels:
            self._channel.addItem("(no public channels found)", None)

    def _send_test(self) -> None:
        selected = self._selected_channel()
        if selected is None:
            QMessageBox.information(self, "Slack", "Select a channel first.")
            return
        self._btn_test.setEnabled(False)
        service = self._service

        def work() -> None:
            try:
                service.send_test_message(selected[0])
                self._test_finished.emit("")
            except Exception as exc:
                self._test_finished.emit(str(exc) or type(exc).__name__)

        threading.Thread(target=work, name="SlackTestMessage", daemon=True).start()

    def _on_test_finished(self, error: str) -> None:
        self._btn_test.setEnabled(True)
        if error:
            QMessageBox.warning(self, "Slack", f"Test message failed:\n{error}")
        else:
            QMessageBox.information(self, "Slack", "Test message sent.")

    def _save(self) -> None:
        try:
            allowed = slack_prefs.parse_user_ids(self._allowed.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Slack", str(exc))
            return
        selected = self._selected_channel()
        if self._enabled.isChecked() and selected is None:
            QMessageBox.warning(self, "Slack", "Select a notification channel first.")
            return
        if self._slash_enabled.isChecked() and not allowed:
            QMessageBox.warning(self, "Slack",
                                "Enter at least one allowed member ID to use slash commands.")
            return
        prefs = SlackPrefs(
            enabled=self._enabled.isChecked(),
            channel_id=selected[0] if selected else "",
            channel_name=selected[1] if selected else "",
            notify_on_completion=self._on_completion.isChecked(),
            notify_on_condition_stop=self._on_condition.isChecked(),
            notify_on_error=self._on_error.isChecked(),
            error_suppress_minutes=self._suppress.value(),
            slash_commands_enabled=self._slash_enabled.isChecked(),
            allowed_user_ids=allowed,
        )
        try:
            slack_prefs.save(prefs)
        except OSError as exc:
            QMessageBox.warning(self, "Slack", f"Could not save the settings:\n{exc}")
            return
        if self._service is not None:
            self._service.update_prefs(prefs)
        self._allowed.setText(", ".join(allowed))
        self._refresh_status()
        QMessageBox.information(self, "Slack", "Slack settings saved.")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_status()

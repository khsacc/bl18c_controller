"""Local Slack notification settings (channel, switches, allowed users)
and the Slack app tokens.

The tokens are kept in their own file (slack_credentials.json) so the
ordinary settings file never contains a secret. Both files live in
__localdata, which is not tracked by git.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

_PREFS_FILE = Path(__file__).parent / "__localdata" / "slack_settings.json"
_CREDENTIALS_FILE = Path(__file__).parent / "__localdata" / "slack_credentials.json"

BOT_TOKEN_PREFIX = "xoxb-"
APP_TOKEN_PREFIX = "xapp-"

DEFAULT_ERROR_SUPPRESS_MINUTES = 10
_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")


@dataclass
class SlackPrefs:
    enabled: bool = False
    channel_id: str = ""
    channel_name: str = ""          # display only; channel_id is what is used
    notify_on_completion: bool = True
    notify_on_condition_stop: bool = True
    notify_on_error: bool = True
    error_suppress_minutes: int = DEFAULT_ERROR_SUPPRESS_MINUTES
    slash_commands_enabled: bool = False
    allowed_user_ids: list[str] = field(default_factory=list)

    def is_user_allowed(self, user_id: str) -> bool:
        return bool(user_id) and user_id in self.allowed_user_ids


def parse_user_ids(text: str) -> list[str]:
    """Split a comma/space separated list of Slack member IDs and validate it."""
    ids: list[str] = []
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        token = token.upper()
        if not _USER_ID_RE.match(token):
            raise ValueError(
                f"'{token}' is not a Slack member ID (expected something like U01ABCDE2FG)"
            )
        if token not in ids:
            ids.append(token)
    return ids


def load() -> SlackPrefs:
    """Return the saved settings, or defaults if missing or unreadable."""
    try:
        data = json.loads(_PREFS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SlackPrefs()
    if not isinstance(data, dict):
        return SlackPrefs()
    prefs = SlackPrefs()
    for f in fields(SlackPrefs):
        if f.name not in data:
            continue
        value = data[f.name]
        default = getattr(prefs, f.name)
        if isinstance(default, bool):
            if isinstance(value, bool):
                setattr(prefs, f.name, value)
        elif isinstance(default, int):
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                setattr(prefs, f.name, value)
        elif isinstance(default, str):
            if isinstance(value, str):
                setattr(prefs, f.name, value)
        elif isinstance(default, list):
            if isinstance(value, list):
                setattr(prefs, f.name, [str(v) for v in value if isinstance(v, str)])
    return prefs


def save(prefs: SlackPrefs) -> None:
    _PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(prefs), indent=2, ensure_ascii=False)
    temporary = _PREFS_FILE.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, _PREFS_FILE)
    finally:
        temporary.unlink(missing_ok=True)


# ── tokens ────────────────────────────────────────────────────────────────────

def validate_tokens(bot_token: str, app_token: str) -> tuple[str, str]:
    """Strip and check the token kinds. Empty means "not set"."""
    bot_token, app_token = bot_token.strip(), app_token.strip()
    for value, prefix, label in ((bot_token, BOT_TOKEN_PREFIX, "Bot User OAuth Token"),
                                 (app_token, APP_TOKEN_PREFIX, "App-Level Token")):
        if not value:
            continue
        if not value.startswith(prefix):
            raise ValueError(f"The {label} must start with '{prefix}'.")
        if any(ch.isspace() for ch in value):
            raise ValueError(f"The {label} must not contain spaces.")
    return bot_token, app_token


def load_credentials() -> tuple[str, str]:
    """Return (bot token, app token) saved from the settings page ("" if none)."""
    try:
        data = json.loads(_CREDENTIALS_FILE.read_text(encoding="utf-8"))
        return validate_tokens(str(data.get("bot_token", "")), str(data.get("app_token", "")))
    except (OSError, ValueError, TypeError, AttributeError):
        return "", ""


def save_credentials(bot_token: str, app_token: str) -> None:
    bot_token, app_token = validate_tokens(bot_token, app_token)
    _CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"bot_token": bot_token, "app_token": app_token}, indent=2)
    # Atomic replacement; owner-only permissions where the OS supports them.
    temporary = _CREDENTIALS_FILE.with_suffix(".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, _CREDENTIALS_FILE)
    finally:
        temporary.unlink(missing_ok=True)

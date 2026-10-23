"""Local STARS connection settings for the BL-18C X-ray stopper."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path

DEFAULT_IP_ADDRESS = "192.168.1.101"
PORT = 6057
NODE_NAME = "Auto18C"
_PREFS_FILE = Path(__file__).parent / "__localdata" / "shutter_connection.json"


def load() -> tuple[str, str]:
    """Return (server IP, keyword). The keyword is never part of tracked files."""
    try:
        data = json.loads(_PREFS_FILE.read_text(encoding="utf-8"))
        ip_address = str(ipaddress.IPv4Address(data["ip_address"]))
        keyword = str(data["keyword"])
        return ip_address, keyword
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_IP_ADDRESS, ""


def save(ip_address: str, keyword: str) -> None:
    ip_address = str(ipaddress.IPv4Address(ip_address.strip()))
    keyword = keyword.strip()
    if not keyword or any(ch.isspace() for ch in keyword):
        raise ValueError("STARS keyword must be a single non-empty word")
    _PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"ip_address": ip_address, "keyword": keyword}, indent=2)
    # Atomic replacement avoids leaving a half-written credential file.
    temporary = _PREFS_FILE.with_suffix(".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, _PREFS_FILE)
    finally:
        temporary.unlink(missing_ok=True)

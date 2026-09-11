"""App-wide connection settings for the online spectrometer service.

The settings are shared by Ruby Finder and Experimental Scheduler.  They are
stored under ``settings/__localdata`` (ignored by git); the API key is never
copied into experiment sequence files or acquisition logs.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_IP_ADDRESS = "192.168.1.102"
DEFAULT_PORT = 8765
_PREFS_FILE = Path(__file__).parent / "__localdata" / "online_spectrometer.json"

_ip_address = DEFAULT_IP_ADDRESS
_api_key = ""
_loaded = False


def _environment_ip_address() -> str:
    explicit = os.environ.get("FLUORA_PRESSEE_IP", "").strip()
    if explicit:
        candidate = explicit
    else:
        candidate = ""
    # Preserve compatibility with the URL environment variable previously
    # accepted by Ruby Finder.
    legacy_url = os.environ.get("FLUORA_PRESSEE_URL", "").strip()
    if not candidate and legacy_url:
        parsed = urlparse(legacy_url)
        if parsed.hostname:
            candidate = parsed.hostname
    try:
        return str(ipaddress.IPv4Address(candidate or DEFAULT_IP_ADDRESS))
    except ipaddress.AddressValueError:
        return DEFAULT_IP_ADDRESS


def load() -> None:
    """Load the persisted connection, falling back to environment/defaults."""
    global _api_key, _ip_address, _loaded
    fallback_ip = _environment_ip_address()
    fallback_key = os.environ.get("FLUORA_PRESSEE_API_KEY", "").strip()
    try:
        with _PREFS_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
        ip_address = str(data.get("ip_address", fallback_ip)).strip()
        # Reject corrupted persisted addresses without making every consumer
        # repeat validation logic.
        ipaddress.IPv4Address(ip_address)
        _ip_address = ip_address
        _api_key = str(data.get("api_key", fallback_key)).strip()
    except Exception:
        _ip_address = fallback_ip
        _api_key = fallback_key
    _loaded = True


def _ensure_loaded() -> None:
    if not _loaded:
        load()


def get_ip_address() -> str:
    _ensure_loaded()
    return _ip_address


def get_api_key() -> str:
    _ensure_loaded()
    return _api_key


def get_base_url() -> str:
    """Return the FluoRaPressée base URL for HTTP clients."""
    return f"http://{get_ip_address()}:{DEFAULT_PORT}"


def set_connection(ip_address: str, api_key: str) -> None:
    """Validate, apply and persist the shared connection settings."""
    global _api_key, _ip_address, _loaded
    ip_address = ip_address.strip()
    api_key = api_key.strip()
    try:
        ipaddress.IPv4Address(ip_address)
    except ipaddress.AddressValueError as exc:
        raise ValueError("IP address must be a valid IPv4 address") from exc
    if not api_key:
        raise ValueError("API key is required")
    _save(ip_address, api_key)
    _ip_address = ip_address
    _api_key = api_key
    _loaded = True


def _save(ip_address: str, api_key: str) -> None:
    _PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _PREFS_FILE.open("w", encoding="utf-8") as fh:
        json.dump(
            {"ip_address": ip_address, "api_key": api_key},
            fh,
            indent=2,
            ensure_ascii=False,
        )

"""
Ollama HTTP client for the LLM DSL generation backend.

All network I/O runs on a QThread (OllamaChatWorker) to avoid blocking the UI.
The /api/chat endpoint is used with stream=False for simplicity; the response
arrives in one piece after the model finishes generating.

Generation parameters are intentionally conservative (low temperature) because
the goal is reproducible DSL output, not creative text.
"""
from __future__ import annotations

import json
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

try:
    import requests as _requests

    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

# Low temperature for reproducible, constraint-following output.
# num_ctx=8192: the system prompt alone is ~4352 tokens; the Ollama default
# of 4096 causes silent truncation.  8192 leaves ~3840 tokens for the reply.
# On CPU (no GPU), prompt processing takes ~60 s and generation ~2–5 min —
# _CHAT_TIMEOUT must be long enough to survive the full round-trip.
_DEFAULT_OPTIONS: dict[str, Any] = {
    "temperature": 0.1,
    "top_p": 0.8,
    "num_predict": 2048,
    "num_ctx": 8192,
}

_CONNECT_TIMEOUT = 5.0    # seconds for the test connection
_CHAT_TIMEOUT = 600.0     # 10 min — CPU inference is slow (no GPU)


class OllamaClientError(RuntimeError):
    """Raised when the Ollama server cannot be reached or returns an error."""


def check_connection(base_url: str) -> list[str]:
    """Test connectivity and return a list of available model names.

    Parameters
    ----------
    base_url : str
        Ollama base URL, e.g. ``"http://localhost:11434"``.

    Returns
    -------
    list[str]
        Names of installed models (e.g. ``["qwen3-coder:14b", ...]``).

    Raises
    ------
    OllamaClientError
        If the server is unreachable or returns unexpected data.
    """
    if not _REQUESTS_AVAILABLE:
        raise OllamaClientError(
            "'requests' package is not installed. "
            "Run:  pip install requests"
        )
    try:
        resp = _requests.get(
            f"{base_url.rstrip('/')}/api/tags",
            timeout=_CONNECT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return [m["name"] for m in data.get("models", [])]
    except _requests.RequestException as exc:
        raise OllamaClientError(str(exc)) from exc
    except (KeyError, json.JSONDecodeError, ValueError) as exc:
        raise OllamaClientError(f"Unexpected response: {exc}") from exc


class OllamaChatWorker(QThread):
    """QThread that sends a chat request to Ollama and emits the response.

    Signals
    -------
    finished(str)
        Emitted with the full assistant response text on success.
    error(str)
        Emitted with an error message on failure.
    """

    finished: pyqtSignal = pyqtSignal(str)
    error: pyqtSignal = pyqtSignal(str)

    def __init__(
        self,
        base_url: str,
        model: str,
        messages: list[dict[str, str]],
        options: dict[str, Any] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._messages = messages
        self._options = {**_DEFAULT_OPTIONS, **(options or {})}

    def run(self) -> None:
        if not _REQUESTS_AVAILABLE:
            self.error.emit(
                "'requests' package is not installed. Run:  pip install requests"
            )
            return

        payload = {
            "model": self._model,
            "messages": self._messages,
            "stream": False,
            "options": self._options,
        }
        try:
            resp = _requests.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=_CHAT_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            content: str = data["message"]["content"]
            self.finished.emit(content)
        except _requests.RequestException as exc:
            self.error.emit(str(exc))
        except (KeyError, json.JSONDecodeError) as exc:
            self.error.emit(f"Unexpected response format: {exc}")


class OllamaConnectionWorker(QThread):
    """QThread for testing connectivity without blocking the UI.

    Signals
    -------
    success(list)
        Emitted with the list of available model names on success.
    error(str)
        Emitted with an error message on failure.
    """

    success: pyqtSignal = pyqtSignal(list)
    error: pyqtSignal = pyqtSignal(str)

    def __init__(self, base_url: str, parent=None) -> None:
        super().__init__(parent)
        self._base_url = base_url

    def run(self) -> None:
        try:
            models = check_connection(self._base_url)
            self.success.emit(models)
        except OllamaClientError as exc:
            self.error.emit(str(exc))
